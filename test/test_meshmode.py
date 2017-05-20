from __future__ import division, absolute_import, print_function

__copyright__ = "Copyright (C) 2014 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from six.moves import range
import numpy as np
import numpy.linalg as la
import pyopencl as cl
import pyopencl.array  # noqa
import pyopencl.clmath  # noqa

from pyopencl.tools import (  # noqa
        pytest_generate_tests_for_pyopencl
        as pytest_generate_tests)

from meshmode.discretization.poly_element import (
        InterpolatoryQuadratureSimplexGroupFactory,
        PolynomialWarpAndBlendGroupFactory,
        PolynomialEquidistantSimplexGroupFactory,
        )
from meshmode.mesh import BTAG_ALL
from meshmode.discretization.connection import \
        FACE_RESTR_ALL, FACE_RESTR_INTERIOR
import meshmode.mesh.generation as mgen

import pytest

import logging
logger = logging.getLogger(__name__)


# {{{ partition_interpolation

@pytest.mark.parametrize("group_factory", [
                            PolynomialWarpAndBlendGroupFactory,
                            #InterpolatoryQuadratureSimplexGroupFactory
                            ])
@pytest.mark.parametrize("num_parts", [2])#, 3])
# FIXME: Mostly fails for multiple groups.
@pytest.mark.parametrize("num_groups", [1])
@pytest.mark.parametrize(("dim", "mesh_pars"), [
         (2, [10, 20, 30]),
         #(3, [3, 5])
        ])
def test_partition_interpolation(ctx_getter, group_factory, dim, mesh_pars,
                                    num_parts, num_groups):
    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)
    order = 3

    from pytools.convergence import EOCRecorder
    eoc_rec = dict()
    for i in range(num_parts):
        for j in range(num_parts):
            if i == j:
                continue
            eoc_rec[(i, j)] = EOCRecorder()

    def f(x):
        return x
        #return 0.1*cl.clmath.sin(30*x)

    for n in mesh_pars:
        from meshmode.mesh.generation import generate_warped_rect_mesh
        meshes = [generate_warped_rect_mesh(dim, order=order, n=n)
                                for _ in range(num_groups)]

        if num_groups > 1:
            from meshmode.mesh.processing import merge_disjoint_meshes
            mesh = merge_disjoint_meshes(meshes)
        else:
            mesh = meshes[0]

        from pymetis import part_graph
        (_, p) = part_graph(num_parts, adjacency=mesh.adjacency_list())
        part_per_element = np.array(p)

        from meshmode.mesh.processing import partition_mesh
        part_meshes = [
            partition_mesh(mesh, part_per_element, i)[0] for i in range(num_parts)]

        from meshmode.discretization import Discretization
        vol_discrs = [Discretization(cl_ctx, part_meshes[i], group_factory(order))
                        for i in range(num_parts)]

        from meshmode.mesh import BTAG_PARTITION
        from meshmode.discretization.connection import (make_face_restriction,
                                                        make_partition_connection,
                                                        check_connection)

        for i_tgt_part in range(num_parts):
            for i_src_part in range(num_parts):
                if i_tgt_part == i_src_part:
                    continue

                # Connections within tgt_mesh to src_mesh
                tgt_to_src_conn = make_face_restriction(vol_discrs[i_tgt_part],
                                                        group_factory(order),
                                                        BTAG_PARTITION(i_src_part))

                # Connections within src_mesh to tgt_mesh
                src_to_tgt_conn = make_face_restriction(vol_discrs[i_src_part],
                                                        group_factory(order),
                                                        BTAG_PARTITION(i_tgt_part))

                # Connect tgt_mesh to src_mesh
                connection = make_partition_connection(tgt_to_src_conn,
                                                       src_to_tgt_conn, i_src_part)

                check_connection(connection)

                # Should this be src_to_tgt_conn?
                bdry_x = tgt_to_src_conn.to_discr.nodes()[0].with_queue(queue)
                if bdry_x.size != 0:
                    bdry_f = f(bdry_x)

                    bdry_f_2 = connection(queue, bdry_f)

                    err = la.norm((bdry_f-bdry_f_2).get(), np.inf)
                    eoc_rec[(i_tgt_part, i_src_part)].add_data_point(1./n, err)

    print(eoc_rec[(0, 1)])

    assert (eoc_rec[(0, 1)].order_estimate() >= order-0.5
            or eoc_rec[(0, 1)].max_error() < 1e-13)

# }}}


# {{{ partition_mesh

@pytest.mark.parametrize("dim", [2, 3])
@pytest.mark.parametrize("num_parts", [4, 5, 7])
@pytest.mark.parametrize("num_meshes", [1, 2, 7])
def test_partition_mesh(num_parts, num_meshes, dim):
    n = (5,) * dim
    from meshmode.mesh.generation import generate_regular_rect_mesh
    meshes = [generate_regular_rect_mesh(a=(0 + i,) * dim, b=(1 + i,) * dim, n=n)
                        for i in range(num_meshes)]

    from meshmode.mesh.processing import merge_disjoint_meshes
    mesh = merge_disjoint_meshes(meshes)

    from pymetis import part_graph
    (_, p) = part_graph(num_parts, adjacency=mesh.adjacency_list())
    part_per_element = np.array(p)

    from meshmode.mesh.processing import partition_mesh
    # TODO: The same part_per_element array must be used to partition each mesh.
    # Maybe the interface should be changed to guarantee this.
    new_meshes = [
        partition_mesh(mesh, part_per_element, i) for i in range(num_parts)]

    assert mesh.nelements == np.sum(
        [new_meshes[i][0].nelements for i in range(num_parts)]), \
        "part_mesh has the wrong number of elements"

    assert count_tags(mesh, BTAG_ALL) == np.sum(
        [count_tags(new_meshes[i][0], BTAG_ALL) for i in range(num_parts)]), \
        "part_mesh has the wrong number of BTAG_ALL boundaries"

    from meshmode.mesh import BTAG_PARTITION
    num_tags = np.zeros((num_parts,))

    for part_num in range(num_parts):
        part, part_to_global = new_meshes[part_num]
        for grp_num, f_groups in enumerate(part.facial_adjacency_groups):
            f_grp = f_groups[None]
            elem_base = part.groups[grp_num].element_nr_base
            for idx, elem in enumerate(f_grp.elements):
                tag = -f_grp.neighbors[idx]
                assert tag >= 0
                face = f_grp.element_faces[idx]
                for n_part_num, adj in part.interpart_adj_groups[grp_num].items():
                    n_part, n_part_to_global = new_meshes[n_part_num]
                    if tag & part.boundary_tag_bit(BTAG_PARTITION(n_part_num)) != 0:
                        num_tags[n_part_num] += 1

                        (n_meshwide_elem, n_face) = adj.get_neighbor(elem, face)
                        n_grp_num = n_part.find_igrp(n_meshwide_elem)
                        n_adj = n_part.interpart_adj_groups[n_grp_num][part_num]
                        n_elem_base = n_part.groups[n_grp_num].element_nr_base
                        n_elem = n_meshwide_elem - n_elem_base
                        assert (elem + elem_base, face) ==\
                                            n_adj.get_neighbor(n_elem, n_face),\
                                            "InterPartitionAdj is not consistent"

                        n_part_to_global = new_meshes[n_part_num][1]
                        p_meshwide_elem = part_to_global[elem + elem_base]
                        p_meshwide_n_elem = n_part_to_global[n_elem + n_elem_base]

                        p_grp_num = mesh.find_igrp(p_meshwide_elem)
                        p_n_grp_num = mesh.find_igrp(p_meshwide_n_elem)

                        p_elem_base = mesh.groups[p_grp_num].element_nr_base
                        p_n_elem_base = mesh.groups[p_n_grp_num].element_nr_base
                        p_elem = p_meshwide_elem - p_elem_base
                        p_n_elem = p_meshwide_n_elem - p_n_elem_base

                        f_groups = mesh.facial_adjacency_groups[p_grp_num]
                        for p_bnd_adj in f_groups.values():
                            for idx in range(len(p_bnd_adj.elements)):
                                if (p_elem == p_bnd_adj.elements[idx] and
                                         face == p_bnd_adj.element_faces[idx]):
                                    assert p_n_elem == p_bnd_adj.neighbors[idx],\
                                            "Tag does not give correct neighbor"
                                    assert n_face == p_bnd_adj.neighbor_faces[idx],\
                                            "Tag does not give correct neighbor"

    for i_tag in range(num_parts):
        tag_sum = 0
        for mesh, _ in new_meshes:
            tag_sum += count_tags(mesh, BTAG_PARTITION(i_tag))
        assert num_tags[i_tag] == tag_sum,\
                "part_mesh has the wrong number of BTAG_PARTITION boundaries"


def count_tags(mesh, tag):
    num_bnds = 0
    for adj_dict in mesh.facial_adjacency_groups:
        for _, bdry_group in adj_dict.items():
            for neighbors in bdry_group.neighbors:
                if neighbors < 0:
                    if -neighbors & mesh.boundary_tag_bit(tag) != 0:
                        num_bnds += 1
    return num_bnds

# }}}


# {{{ circle mesh

def test_circle_mesh(do_plot=False):
    from meshmode.mesh.io import generate_gmsh, FileSource
    print("BEGIN GEN")
    mesh = generate_gmsh(
            FileSource("circle.step"), 2, order=2,
            force_ambient_dim=2,
            other_options=[
                "-string", "Mesh.CharacteristicLengthMax = 0.05;"]
            )
    print("END GEN")
    print(mesh.nelements)

    from meshmode.mesh.processing import affine_map
    mesh = affine_map(mesh, A=3*np.eye(2))

    if do_plot:
        from meshmode.mesh.visualization import draw_2d_mesh
        draw_2d_mesh(mesh, fill=None, draw_nodal_adjacency=True,
                set_bounding_box=True)
        import matplotlib.pyplot as pt
        pt.show()

# }}}


# {{{ convergence of boundary interpolation

@pytest.mark.parametrize("group_factory", [
    InterpolatoryQuadratureSimplexGroupFactory,
    PolynomialWarpAndBlendGroupFactory
    ])
@pytest.mark.parametrize("boundary_tag", [
    BTAG_ALL,
    FACE_RESTR_ALL,
    FACE_RESTR_INTERIOR,
    ])
@pytest.mark.parametrize(("mesh_name", "dim", "mesh_pars"), [
    ("blob", 2, [1e-1, 8e-2, 5e-2]),
    ("warp", 2, [10, 20, 30]),
    ("warp", 3, [10, 20, 30]),
    ])
@pytest.mark.parametrize("per_face_groups", [False, True])
def test_boundary_interpolation(ctx_getter, group_factory, boundary_tag,
        mesh_name, dim, mesh_pars, per_face_groups):
    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)

    from meshmode.discretization import Discretization
    from meshmode.discretization.connection import (
            make_face_restriction, check_connection)

    from pytools.convergence import EOCRecorder
    eoc_rec = EOCRecorder()

    order = 4

    def f(x):
        return 0.1*cl.clmath.sin(30*x)

    for mesh_par in mesh_pars:
        # {{{ get mesh

        if mesh_name == "blob":
            assert dim == 2

            h = mesh_par

            from meshmode.mesh.io import generate_gmsh, FileSource
            print("BEGIN GEN")
            mesh = generate_gmsh(
                    FileSource("blob-2d.step"), 2, order=order,
                    force_ambient_dim=2,
                    other_options=[
                        "-string", "Mesh.CharacteristicLengthMax = %s;" % h]
                    )
            print("END GEN")
        elif mesh_name == "warp":
            from meshmode.mesh.generation import generate_warped_rect_mesh
            mesh = generate_warped_rect_mesh(dim, order=4, n=mesh_par)

            h = 1/mesh_par
        else:
            raise ValueError("mesh_name not recognized")

        # }}}

        vol_discr = Discretization(cl_ctx, mesh,
                group_factory(order))
        print("h=%s -> %d elements" % (
                h, sum(mgrp.nelements for mgrp in mesh.groups)))

        x = vol_discr.nodes()[0].with_queue(queue)
        vol_f = f(x)

        bdry_connection = make_face_restriction(
                vol_discr, group_factory(order),
                boundary_tag, per_face_groups=per_face_groups)
        check_connection(bdry_connection)
        bdry_discr = bdry_connection.to_discr

        bdry_x = bdry_discr.nodes()[0].with_queue(queue)
        bdry_f = f(bdry_x)
        bdry_f_2 = bdry_connection(queue, vol_f)

        if mesh_name == "blob" and dim == 2:
            mat = bdry_connection.full_resample_matrix(queue).get(queue)
            bdry_f_2_by_mat = mat.dot(vol_f.get())

            mat_error = la.norm(bdry_f_2.get(queue=queue) - bdry_f_2_by_mat)
            assert mat_error < 1e-14, mat_error

        err = la.norm((bdry_f-bdry_f_2).get(), np.inf)
        eoc_rec.add_data_point(h, err)

    print(eoc_rec)
    assert (
            eoc_rec.order_estimate() >= order-0.5
            or eoc_rec.max_error() < 1e-14)

# }}}


# {{{ boundary-to-all-faces connecttion

@pytest.mark.parametrize(("mesh_name", "dim", "mesh_pars"), [
    ("blob", 2, [1e-1, 8e-2, 5e-2]),
    ("warp", 2, [10, 20, 30]),
    ("warp", 3, [10, 20, 30]),
    ])
@pytest.mark.parametrize("per_face_groups", [False, True])
def test_all_faces_interpolation(ctx_getter, mesh_name, dim, mesh_pars,
        per_face_groups):
    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)

    from meshmode.discretization import Discretization
    from meshmode.discretization.connection import (
            make_face_restriction, make_face_to_all_faces_embedding,
            check_connection)

    from pytools.convergence import EOCRecorder
    eoc_rec = EOCRecorder()

    order = 4

    def f(x):
        return 0.1*cl.clmath.sin(30*x)

    for mesh_par in mesh_pars:
        # {{{ get mesh

        if mesh_name == "blob":
            assert dim == 2

            h = mesh_par

            from meshmode.mesh.io import generate_gmsh, FileSource
            print("BEGIN GEN")
            mesh = generate_gmsh(
                    FileSource("blob-2d.step"), 2, order=order,
                    force_ambient_dim=2,
                    other_options=[
                        "-string", "Mesh.CharacteristicLengthMax = %s;" % h]
                    )
            print("END GEN")
        elif mesh_name == "warp":
            from meshmode.mesh.generation import generate_warped_rect_mesh
            mesh = generate_warped_rect_mesh(dim, order=4, n=mesh_par)

            h = 1/mesh_par
        else:
            raise ValueError("mesh_name not recognized")

        # }}}

        vol_discr = Discretization(cl_ctx, mesh,
                PolynomialWarpAndBlendGroupFactory(order))
        print("h=%s -> %d elements" % (
                h, sum(mgrp.nelements for mgrp in mesh.groups)))

        all_face_bdry_connection = make_face_restriction(
                vol_discr, PolynomialWarpAndBlendGroupFactory(order),
                FACE_RESTR_ALL, per_face_groups=per_face_groups)
        all_face_bdry_discr = all_face_bdry_connection.to_discr

        for ito_grp, ceg in enumerate(all_face_bdry_connection.groups):
            for ibatch, batch in enumerate(ceg.batches):
                assert np.array_equal(
                        batch.from_element_indices.get(queue),
                        np.arange(vol_discr.mesh.nelements))

                if per_face_groups:
                    assert ito_grp == batch.to_element_face
                else:
                    assert ibatch == batch.to_element_face

        all_face_x = all_face_bdry_discr.nodes()[0].with_queue(queue)
        all_face_f = f(all_face_x)

        all_face_f_2 = all_face_bdry_discr.zeros(queue)

        for boundary_tag in [
                BTAG_ALL,
                FACE_RESTR_INTERIOR,
                ]:
            bdry_connection = make_face_restriction(
                    vol_discr, PolynomialWarpAndBlendGroupFactory(order),
                    boundary_tag, per_face_groups=per_face_groups)
            bdry_discr = bdry_connection.to_discr

            bdry_x = bdry_discr.nodes()[0].with_queue(queue)
            bdry_f = f(bdry_x)

            all_face_embedding = make_face_to_all_faces_embedding(
                    bdry_connection, all_face_bdry_discr)

            check_connection(all_face_embedding)

            all_face_f_2 += all_face_embedding(queue, bdry_f)

        err = la.norm((all_face_f-all_face_f_2).get(), np.inf)
        eoc_rec.add_data_point(h, err)

    print(eoc_rec)
    assert (
            eoc_rec.order_estimate() >= order-0.5
            or eoc_rec.max_error() < 1e-14)

# }}}


# {{{ convergence of opposite-face interpolation

@pytest.mark.parametrize("group_factory", [
    InterpolatoryQuadratureSimplexGroupFactory,
    PolynomialWarpAndBlendGroupFactory
    ])
@pytest.mark.parametrize(("mesh_name", "dim", "mesh_pars"), [
    ("blob", 2, [1e-1, 8e-2, 5e-2]),
    ("warp", 2, [3, 5, 7]),
    ("warp", 3, [3, 5]),
    ])
def test_opposite_face_interpolation(ctx_getter, group_factory,
        mesh_name, dim, mesh_pars):
    logging.basicConfig(level=logging.INFO)

    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)

    from meshmode.discretization import Discretization
    from meshmode.discretization.connection import (
            make_face_restriction, make_opposite_face_connection,
            check_connection)

    from pytools.convergence import EOCRecorder
    eoc_rec = EOCRecorder()

    order = 5

    def f(x):
        return 0.1*cl.clmath.sin(30*x)

    for mesh_par in mesh_pars:
        # {{{ get mesh

        if mesh_name == "blob":
            assert dim == 2

            h = mesh_par

            from meshmode.mesh.io import generate_gmsh, FileSource
            print("BEGIN GEN")
            mesh = generate_gmsh(
                    FileSource("blob-2d.step"), 2, order=order,
                    force_ambient_dim=2,
                    other_options=[
                        "-string", "Mesh.CharacteristicLengthMax = %s;" % h]
                    )
            print("END GEN")
        elif mesh_name == "warp":
            from meshmode.mesh.generation import generate_warped_rect_mesh
            mesh = generate_warped_rect_mesh(dim, order=4, n=mesh_par)

            h = 1/mesh_par
        else:
            raise ValueError("mesh_name not recognized")

        # }}}

        vol_discr = Discretization(cl_ctx, mesh,
                group_factory(order))
        print("h=%s -> %d elements" % (
                h, sum(mgrp.nelements for mgrp in mesh.groups)))

        bdry_connection = make_face_restriction(
                vol_discr, group_factory(order),
                FACE_RESTR_INTERIOR)
        bdry_discr = bdry_connection.to_discr

        opp_face = make_opposite_face_connection(bdry_connection)
        check_connection(opp_face)

        bdry_x = bdry_discr.nodes()[0].with_queue(queue)
        bdry_f = f(bdry_x)

        bdry_f_2 = opp_face(queue, bdry_f)

        err = la.norm((bdry_f-bdry_f_2).get(), np.inf)
        eoc_rec.add_data_point(h, err)

    print(eoc_rec)
    assert (
            eoc_rec.order_estimate() >= order-0.5
            or eoc_rec.max_error() < 1e-13)

# }}}


# {{{ element orientation

def test_element_orientation():
    from meshmode.mesh.io import generate_gmsh, FileSource

    mesh_order = 3

    mesh = generate_gmsh(
            FileSource("blob-2d.step"), 2, order=mesh_order,
            force_ambient_dim=2,
            other_options=["-string", "Mesh.CharacteristicLengthMax = 0.02;"]
            )

    from meshmode.mesh.processing import (perform_flips,
            find_volume_mesh_element_orientations)
    mesh_orient = find_volume_mesh_element_orientations(mesh)

    assert (mesh_orient > 0).all()

    from random import randrange
    flippy = np.zeros(mesh.nelements, np.int8)
    for i in range(int(0.3*mesh.nelements)):
        flippy[randrange(0, mesh.nelements)] = 1

    mesh = perform_flips(mesh, flippy, skip_tests=True)

    mesh_orient = find_volume_mesh_element_orientations(mesh)

    assert ((mesh_orient < 0) == (flippy > 0)).all()

# }}}


# {{{ element orientation: canned 3D meshes

# python test_meshmode.py 'test_sanity_balls(cl._csc, "disk-radius-1.step", 2, 2, visualize=True)'  # noqa
@pytest.mark.parametrize(("what", "mesh_gen_func"), [
    ("ball", lambda: mgen.generate_icosahedron(1, 1)),
    ("torus", lambda: mgen.generate_torus(5, 1)),
    ])
def test_3d_orientation(ctx_getter, what, mesh_gen_func, visualize=False):
    pytest.importorskip("pytential")

    logging.basicConfig(level=logging.INFO)

    ctx = ctx_getter()
    queue = cl.CommandQueue(ctx)

    mesh = mesh_gen_func()

    logger.info("%d elements" % mesh.nelements)

    from meshmode.discretization import Discretization
    discr = Discretization(ctx, mesh,
            PolynomialWarpAndBlendGroupFactory(1))

    from pytential import bind, sym

    # {{{ check normals point outward

    if what == "torus":
        nodes = sym.nodes(mesh.ambient_dim).as_vector()
        angle = sym.atan2(nodes[1], nodes[0])
        center_nodes = sym.make_obj_array([
                5*sym.cos(angle),
                5*sym.sin(angle),
                0*angle])
        normal_outward_expr = (
                sym.normal(mesh.ambient_dim) | (nodes-center_nodes))

    else:
        normal_outward_expr = (
                sym.normal(mesh.ambient_dim) | sym.nodes(mesh.ambient_dim))

    normal_outward_check = bind(discr, normal_outward_expr)(queue).as_scalar() > 0

    assert normal_outward_check.get().all(), normal_outward_check.get()

    # }}}

    normals = bind(discr, sym.normal(mesh.ambient_dim).xproject(1))(queue)

    if visualize:
        from meshmode.discretization.visualization import make_visualizer
        vis = make_visualizer(queue, discr, 1)

        vis.write_vtk_file("normals.vtu", [
            ("normals", normals),
            ])

# }}}


# {{{ merge and map

def test_merge_and_map(ctx_getter, visualize=False):
    from meshmode.mesh.io import generate_gmsh, FileSource
    from meshmode.mesh.generation import generate_box_mesh
    from meshmode.mesh import TensorProductElementGroup
    from meshmode.discretization.poly_element import (
            PolynomialWarpAndBlendGroupFactory,
            LegendreGaussLobattoTensorProductGroupFactory)

    mesh_order = 3

    if 1:
        mesh = generate_gmsh(
                FileSource("blob-2d.step"), 2, order=mesh_order,
                force_ambient_dim=2,
                other_options=["-string", "Mesh.CharacteristicLengthMax = 0.02;"]
                )

        discr_grp_factory = PolynomialWarpAndBlendGroupFactory(3)
    else:
        mesh = generate_box_mesh(
                (
                    np.linspace(0, 1, 4),
                    np.linspace(0, 1, 4),
                    np.linspace(0, 1, 4),
                    ),
                10, group_factory=TensorProductElementGroup)

        discr_grp_factory = LegendreGaussLobattoTensorProductGroupFactory(3)

    from meshmode.mesh.processing import merge_disjoint_meshes, affine_map
    mesh2 = affine_map(mesh,
            A=np.eye(mesh.ambient_dim),
            b=np.array([5, 0, 0])[:mesh.ambient_dim])

    mesh3 = merge_disjoint_meshes((mesh2, mesh))
    mesh3.facial_adjacency_groups

    mesh3.copy()

    if visualize:
        from meshmode.discretization import Discretization
        cl_ctx = ctx_getter()
        queue = cl.CommandQueue(cl_ctx)

        discr = Discretization(cl_ctx, mesh3, discr_grp_factory)

        from meshmode.discretization.visualization import make_visualizer
        vis = make_visualizer(queue, discr, 3, element_shrink_factor=0.8)
        vis.write_vtk_file("merged.vtu", [])

# }}}


# {{{ sanity checks: single element

@pytest.mark.parametrize("dim", [2, 3])
@pytest.mark.parametrize("order", [1, 3])
def test_sanity_single_element(ctx_getter, dim, order, visualize=False):
    pytest.importorskip("pytential")

    cl_ctx = ctx_getter()
    queue = cl.CommandQueue(cl_ctx)

    from modepy.tools import unit_vertices
    vertices = unit_vertices(dim).T.copy()

    center = np.empty(dim, np.float64)
    center.fill(-0.5)

    import modepy as mp
    from meshmode.mesh import SimplexElementGroup, Mesh, BTAG_ALL
    mg = SimplexElementGroup(
            order=order,
            vertex_indices=np.arange(dim+1, dtype=np.int32).reshape(1, -1),
            nodes=mp.warp_and_blend_nodes(dim, order).reshape(dim, 1, -1),
            dim=dim)

    mesh = Mesh(vertices, [mg], nodal_adjacency=None, facial_adjacency_groups=None)

    from meshmode.discretization import Discretization
    from meshmode.discretization.poly_element import \
            PolynomialWarpAndBlendGroupFactory
    vol_discr = Discretization(cl_ctx, mesh,
            PolynomialWarpAndBlendGroupFactory(order+3))

    # {{{ volume calculation check

    vol_x = vol_discr.nodes().with_queue(queue)

    vol_one = vol_x[0].copy()
    vol_one.fill(1)
    from pytential import norm, integral  # noqa

    from pytools import factorial
    true_vol = 1/factorial(dim) * 2**dim

    comp_vol = integral(vol_discr, queue, vol_one)
    rel_vol_err = abs(true_vol - comp_vol) / true_vol

    assert rel_vol_err < 1e-12

    # }}}

    # {{{ boundary discretization

    from meshmode.discretization.connection import make_face_restriction
    bdry_connection = make_face_restriction(
            vol_discr, PolynomialWarpAndBlendGroupFactory(order + 3),
            BTAG_ALL)
    bdry_discr = bdry_connection.to_discr

    # }}}

    # {{{ visualizers

    from meshmode.discretization.visualization import make_visualizer
    #vol_vis = make_visualizer(queue, vol_discr, 4)
    bdry_vis = make_visualizer(queue, bdry_discr, 4)

    # }}}

    from pytential import bind, sym
    bdry_normals = bind(bdry_discr, sym.normal(dim))(queue).as_vector(dtype=object)

    if visualize:
        bdry_vis.write_vtk_file("boundary.vtu", [
            ("bdry_normals", bdry_normals)
            ])

    normal_outward_check = bind(bdry_discr,
            sym.normal(dim)
            |
            (sym.nodes(dim) + 0.5*sym.ones_vec(dim)),
            )(queue).as_scalar() > 0

    assert normal_outward_check.get().all(), normal_outward_check.get()

# }}}


# {{{ sanity check: volume interpolation on scipy/qhull delaunay meshes in nD

@pytest.mark.parametrize("dim", [2, 3, 4])
@pytest.mark.parametrize("order", [3])
def test_sanity_qhull_nd(ctx_getter, dim, order):
    pytest.importorskip("scipy")

    logging.basicConfig(level=logging.INFO)

    ctx = ctx_getter()
    queue = cl.CommandQueue(ctx)

    from scipy.spatial import Delaunay
    verts = np.random.rand(1000, dim)
    dtri = Delaunay(verts)

    from meshmode.mesh.io import from_vertices_and_simplices
    mesh = from_vertices_and_simplices(dtri.points.T, dtri.simplices,
            fix_orientation=True)

    from meshmode.discretization import Discretization
    low_discr = Discretization(ctx, mesh,
            PolynomialEquidistantSimplexGroupFactory(order))
    high_discr = Discretization(ctx, mesh,
            PolynomialEquidistantSimplexGroupFactory(order+1))

    from meshmode.discretization.connection import make_same_mesh_connection
    cnx = make_same_mesh_connection(high_discr, low_discr)

    def f(x):
        return 0.1*cl.clmath.sin(x)

    x_low = low_discr.nodes()[0].with_queue(queue)
    f_low = f(x_low)

    x_high = high_discr.nodes()[0].with_queue(queue)
    f_high_ref = f(x_high)

    f_high_num = cnx(queue, f_low)

    err = (f_high_ref-f_high_num).get()

    err = la.norm(err, np.inf)/la.norm(f_high_ref.get(), np.inf)

    print(err)
    assert err < 1e-2

# }}}


# {{{ sanity checks: ball meshes

# python test_meshmode.py 'test_sanity_balls(cl._csc, "disk-radius-1.step", 2, 2, visualize=True)'  # noqa
@pytest.mark.parametrize(("src_file", "dim"), [
    ("disk-radius-1.step", 2),
    ("ball-radius-1.step", 3),
    ])
@pytest.mark.parametrize("mesh_order", [1, 2])
def test_sanity_balls(ctx_getter, src_file, dim, mesh_order,
        visualize=False):
    pytest.importorskip("pytential")

    logging.basicConfig(level=logging.INFO)

    ctx = ctx_getter()
    queue = cl.CommandQueue(ctx)

    from pytools.convergence import EOCRecorder
    vol_eoc_rec = EOCRecorder()
    surf_eoc_rec = EOCRecorder()

    # overkill
    quad_order = mesh_order

    from pytential import bind, sym

    for h in [0.2, 0.14, 0.1]:
        from meshmode.mesh.io import generate_gmsh, FileSource
        mesh = generate_gmsh(
                FileSource(src_file), dim, order=mesh_order,
                other_options=["-string", "Mesh.CharacteristicLengthMax = %g;" % h],
                force_ambient_dim=dim)

        logger.info("%d elements" % mesh.nelements)

        # {{{ discretizations and connections

        from meshmode.discretization import Discretization
        vol_discr = Discretization(ctx, mesh,
                InterpolatoryQuadratureSimplexGroupFactory(quad_order))

        from meshmode.discretization.connection import make_face_restriction
        bdry_connection = make_face_restriction(
                vol_discr,
                InterpolatoryQuadratureSimplexGroupFactory(quad_order),
                BTAG_ALL)
        bdry_discr = bdry_connection.to_discr

        # }}}

        # {{{ visualizers

        from meshmode.discretization.visualization import make_visualizer
        vol_vis = make_visualizer(queue, vol_discr, 20)
        bdry_vis = make_visualizer(queue, bdry_discr, 20)

        # }}}

        from math import gamma
        true_surf = 2*np.pi**(dim/2)/gamma(dim/2)
        true_vol = true_surf/dim

        vol_x = vol_discr.nodes().with_queue(queue)

        vol_one = vol_x[0].copy()
        vol_one.fill(1)
        from pytential import norm, integral  # noqa

        comp_vol = integral(vol_discr, queue, vol_one)
        rel_vol_err = abs(true_vol - comp_vol) / true_vol
        vol_eoc_rec.add_data_point(h, rel_vol_err)
        print("VOL", true_vol, comp_vol)

        bdry_x = bdry_discr.nodes().with_queue(queue)

        bdry_one_exact = bdry_x[0].copy()
        bdry_one_exact.fill(1)

        bdry_one = bdry_connection(queue, vol_one).with_queue(queue)
        intp_err = norm(bdry_discr, queue, bdry_one-bdry_one_exact)
        assert intp_err < 1e-14

        comp_surf = integral(bdry_discr, queue, bdry_one)
        rel_surf_err = abs(true_surf - comp_surf) / true_surf
        surf_eoc_rec.add_data_point(h, rel_surf_err)
        print("SURF", true_surf, comp_surf)

        if visualize:
            vol_vis.write_vtk_file("volume-h=%g.vtu" % h, [
                ("f", vol_one),
                ("area_el", bind(vol_discr, sym.area_element())(queue)),
                ])
            bdry_vis.write_vtk_file("boundary-h=%g.vtu" % h, [("f", bdry_one)])

        # {{{ check normals point outward

        normal_outward_check = bind(bdry_discr,
                sym.normal(mesh.ambient_dim) | sym.nodes(mesh.ambient_dim),
                )(queue).as_scalar() > 0

        assert normal_outward_check.get().all(), normal_outward_check.get()

        # }}}

    print("---------------------------------")
    print("VOLUME")
    print("---------------------------------")
    print(vol_eoc_rec)
    assert vol_eoc_rec.order_estimate() >= mesh_order

    print("---------------------------------")
    print("SURFACE")
    print("---------------------------------")
    print(surf_eoc_rec)
    assert surf_eoc_rec.order_estimate() >= mesh_order

# }}}


# {{{ rect/box mesh generation

def test_rect_mesh(do_plot=False):
    from meshmode.mesh.generation import generate_regular_rect_mesh
    mesh = generate_regular_rect_mesh()

    if do_plot:
        from meshmode.mesh.visualization import draw_2d_mesh
        draw_2d_mesh(mesh, fill=None, draw_nodal_adjacency=True)
        import matplotlib.pyplot as pt
        pt.show()


def test_box_mesh(ctx_getter, visualize=False):
    from meshmode.mesh.generation import generate_box_mesh
    mesh = generate_box_mesh(3*(np.linspace(0, 1, 5),))

    if visualize:
        from meshmode.discretization import Discretization
        from meshmode.discretization.poly_element import \
                PolynomialWarpAndBlendGroupFactory
        cl_ctx = ctx_getter()
        queue = cl.CommandQueue(cl_ctx)

        discr = Discretization(cl_ctx, mesh,
                PolynomialWarpAndBlendGroupFactory(1))

        from meshmode.discretization.visualization import make_visualizer
        vis = make_visualizer(queue, discr, 1)
        vis.write_vtk_file("box.vtu", [])

# }}}


def test_mesh_copy():
    from meshmode.mesh.generation import generate_box_mesh
    mesh = generate_box_mesh(3*(np.linspace(0, 1, 5),))
    mesh.copy()


# {{{ as_python stringification

def test_as_python():
    from meshmode.mesh.generation import generate_box_mesh
    mesh = generate_box_mesh(3*(np.linspace(0, 1, 5),))

    # These implicitly compute these adjacency structures.
    mesh.nodal_adjacency
    mesh.facial_adjacency_groups

    from meshmode.mesh import as_python
    code = as_python(mesh)

    print(code)
    exec_dict = {}
    exec(compile(code, "gen_code.py", "exec"), exec_dict)

    mesh_2 = exec_dict["make_mesh"]()

    assert mesh == mesh_2

# }}}


# {{{ test lookup tree for element finding

def test_lookup_tree(do_plot=False):
    from meshmode.mesh.generation import make_curve_mesh, cloverleaf
    mesh = make_curve_mesh(cloverleaf, np.linspace(0, 1, 1000), order=3)

    from meshmode.mesh.tools import make_element_lookup_tree
    tree = make_element_lookup_tree(mesh)

    from meshmode.mesh.processing import find_bounding_box
    bbox_min, bbox_max = find_bounding_box(mesh)

    extent = bbox_max-bbox_min

    for i in range(20):
        pt = bbox_min + np.random.rand(2) * extent
        print(pt)
        for igrp, iel in tree.generate_matches(pt):
            print(igrp, iel)

    if do_plot:
        with open("tree.dat", "w") as outf:
            tree.visualize(outf)

# }}}


# {{{ test_nd_quad_submesh

@pytest.mark.parametrize("dims", [2, 3, 4])
def test_nd_quad_submesh(dims):
    from meshmode.mesh.tools import nd_quad_submesh
    from pytools import generate_nonnegative_integer_tuples_below as gnitb

    node_tuples = list(gnitb(3, dims))

    for i, nt in enumerate(node_tuples):
        print(i, nt)

    assert len(node_tuples) == 3**dims

    elements = nd_quad_submesh(node_tuples)

    for e in elements:
        print(e)

    assert len(elements) == 2**dims

# }}}


# {{{ test_quad_mesh_2d

def test_quad_mesh_2d():
    from meshmode.mesh.io import generate_gmsh, ScriptWithFilesSource
    print("BEGIN GEN")
    mesh = generate_gmsh(
            ScriptWithFilesSource(
                """
                Merge "blob-2d.step";
                Mesh.CharacteristicLengthMax = 0.05;
                Recombine Surface "*" = 0.0001;
                Mesh 2;
                Save "output.msh";
                """,
                ["blob-2d.step"]),
            force_ambient_dim=2,
            )
    print("END GEN")
    print(mesh.nelements)

# }}}


# {{{ test_quad_mesh_3d

# This currently (gmsh 2.13.2) crashes gmsh. A massaged version of this using
# 'cube.step' succeeded in generating 'hybrid-cube.msh' and 'cubed-cube.msh'.
def no_test_quad_mesh_3d():
    from meshmode.mesh.io import generate_gmsh, ScriptWithFilesSource
    print("BEGIN GEN")
    mesh = generate_gmsh(
            ScriptWithFilesSource(
                """
                Merge "ball-radius-1.step";
                // Mesh.CharacteristicLengthMax = 0.1;

                Mesh.RecombineAll=1;
                Mesh.Recombine3DAll=1;
                Mesh.Algorithm = 8;
                Mesh.Algorithm3D = 9;
                // Mesh.Smoothing = 0;

                // Mesh.ElementOrder = 3;

                Mesh 3;
                Save "output.msh";
                """,
                ["ball-radius-1.step"]),
            )
    print("END GEN")
    print(mesh.nelements)

# }}}


def test_quad_single_element():
    from meshmode.mesh.generation import make_group_from_vertices
    from meshmode.mesh import Mesh, TensorProductElementGroup

    vertices = np.array([
                [0.91, 1.10],
                [2.64, 1.27],
                [0.97, 2.56],
                [3.00, 3.41],
                ]).T
    mg = make_group_from_vertices(
            vertices,
            np.array([[0, 1, 2, 3]], dtype=np.int32),
            30, group_factory=TensorProductElementGroup)

    Mesh(vertices, [mg], nodal_adjacency=None, facial_adjacency_groups=None)
    if 0:
        import matplotlib.pyplot as plt
        plt.plot(
                mg.nodes[0].reshape(-1),
                mg.nodes[1].reshape(-1), "o")
        plt.show()


def test_quad_multi_element():
    from meshmode.mesh.generation import generate_box_mesh
    from meshmode.mesh import TensorProductElementGroup
    mesh = generate_box_mesh(
            (
                np.linspace(3, 8, 4),
                np.linspace(3, 8, 4),
                np.linspace(3, 8, 4),
                ),
            10, group_factory=TensorProductElementGroup)

    if 0:
        import matplotlib.pyplot as plt
        mg = mesh.groups[0]
        plt.plot(
                mg.nodes[0].reshape(-1),
                mg.nodes[1].reshape(-1), "o")
        plt.show()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from py.test.cmdline import main
        main([__file__])

# vim: fdm=marker
