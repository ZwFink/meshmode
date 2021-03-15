__copyright__ = """
Copyright (C) 2013-2021 Andreas Kloeckner
Copyright (C) 2021 University of Illinois Board of Trustees
"""

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

import numpy as np

from pytools import memoize_in, memoize_method
from meshmode.array_context import ArrayContext, make_loopy_program

# underscored because it shouldn't be imported from here.
from meshmode.dof_array import DOFArray as _DOFArray

__doc__ = """
.. autoclass:: ElementGroupBase
.. autoclass:: ModalElementGroupBase
.. autoclass:: NodalElementGroupBase
.. autoclass:: InterpolatoryElementGroupBase
.. autoclass:: DiscretizationBase
"""


# {{{ element group base

class NoninterpolatoryElementGroupError(TypeError):
    pass


class ElementGroupBase:
    """Container for data of any subclass of :class:`DiscretizationBase`
    corresponding to one :class:`meshmode.mesh.MeshElementGroup`.

    .. attribute :: mesh_el_group
    .. attribute :: order
    .. attribute :: index

    .. autoattribute:: nelements
    .. autoattribute:: dim

    .. attribute:: is_affine

        A :class:`bool` flag that is *True* if the local-to-global
        parametrization of all the elements in the group is affine. Based on
        :attr:`meshmode.mesh.MeshElementGroup.is_affine`.
    """

    def __init__(self, mesh_el_group, order, index):
        """
        :arg mesh_el_group: an instance of
            :class:`meshmode.mesh.MeshElementGroup`
        """
        self.mesh_el_group = mesh_el_group
        self.order = order
        self.index = index

    @property
    def is_affine(self):
        return self.mesh_el_group.is_affine

    @property
    def nelements(self):
        return self.mesh_el_group.nelements

    @property
    def dim(self):
        return self.mesh_el_group.dim

    @property
    def unit_nodes(self):
        raise NotImplementedError

    def basis(self):
        raise NoninterpolatoryElementGroupError("'{}' "
                "is not equipped with a unisolvent function space "
                "and therefore cannot be used for interpolation"
                .format(self.__class__.__name__))

    grad_basis = basis
    diff_matrices = basis


class ModalElementGroupBase(ElementGroupBase):
    """Container for the :class:`ModalDiscretization` data corresponding to
    one :class:`meshmode.mesh.MeshElementGroup`.

    .. method:: orthonormal_basis()

        Returns a :class:`list` of orthonormal basis functions that take
        arrays of shape ``(dim, n)`` and return an array of shape (n,)``
        (which performs evaluation of the basis function).

    .. method:: grad_orthonormal_basis()

        Returns a :class:`list` of functions, each of which
        accepts arrays of shape *(dims, npts)* and returns a
        :class:`tuple` of length *dims* containing the
        derivatives of the orthonormal basis along each axis as an
        array of size *npts*.  'Scalar' evaluation, by passing just one
        vector of length *dims*, is also supported.

    .. method:: mode_ids()

        Returns a tuple of mode (basis function) identifiers, one for
        each basis function.
    """

    @property
    @memoize_method
    def _orthonormal_basis(self):
        import modepy as mp
        return mp.orthonormal_basis_for_space(self._space, self._shape)

    def orthonormal_basis(self):
        return self._orthonormal_basis.functions

    def grad_orthonormal_basis(self):
        return self._orthonormal_basis.gradients

    def mode_ids(self):
        return self._orthonormal_basis.mode_ids

# }}}


# {{{ interpolatory element group base

class NodalElementGroupBase(ElementGroupBase):
    """Container for the :class:`NodalDiscretization` data corresponding to
    one :class:`meshmode.mesh.MeshElementGroup`.

    .. autoattribute:: nunit_dofs
    .. autoattribute:: ndofs

    .. attribute:: unit_nodes

        Returns a :class:`numpy.ndarray` of shape ``(dim, nunit_dofs)``
        of reference coordinates of interpolation nodes.

    .. attribute:: weights

        Returns an array of length :attr:`nunit_dofs` containing
        quadrature weights.
    """

    @property
    def nunit_dofs(self):
        """The number of (nodal) degrees of freedom ("DOFs")
        associated with a single element.
        """
        return self.unit_nodes.shape[-1]

    @property
    def ndofs(self):
        """The total number of (nodal) degrees of freedom ("DOFs")
        associated with the element group.
        """
        return self.nunit_dofs * self.nelements


class InterpolatoryElementGroupBase(NodalElementGroupBase):
    """A subclass of :class:`NodalElementGroupBase` that is equipped with a
    function space.

    .. method:: mode_ids()

        Return an immutable sequence of opaque (hashable) mode identifiers,
        one per element of the :meth:`basis`. The meaning of the mode
        identifiers is defined by the concrete element group.

    .. method:: basis()

        Returns a :class:`list` of basis functions that take arrays
        of shape ``(dim, n)`` and return an array of shape (n,)``
        (which performs evaluation of the basis function).

    .. method:: grad_basis()

        :returns: a :class:`tuple` of functions, each of which
            accepts arrays of shape *(dims, npts)* and returns a
            :class:`tuple` of length *dims* containing the
            derivatives along each axis as an array of size
            *npts*.  'Scalar' evaluation, by passing just one
            vector of length *dims*, is also supported.

    .. method:: diff_matrices()

        Return a :attr:`~ElementGroupBase.dim`-long :class:`tuple` of matrices of
        shape ``(nunit_nodes, nunit_nodes)``, each of which,
        when applied to an array of nodal values, take derivatives
        in the reference (r,s,t) directions.
    """

# }}}


class DiscretizationBase:
    """An unstructured composite discretization base class.

    .. attribute:: real_dtype

    .. attribute:: complex_dtype

    .. attribute:: mesh

    .. attribute:: dim

    .. attribute:: ambient_dim

    .. attribute:: ndofs

    .. attribute :: groups

    .. automethod:: copy
    .. automethod:: empty
    .. automethod:: zeros
    .. automethod:: empty_like
    .. automethod:: zeros_like

    .. automethod:: num_reference_derivative

    .. automethod:: quad_weights
    """

    def __init__(self, actx: ArrayContext, mesh, group_factory,
            real_dtype=np.float64):
        """
        :param actx: A :class:`ArrayContext` used to perform computation needed
            during initial set-up of the mesh.
        :param mesh: A :class:`meshmode.mesh.Mesh` over which the discretization is
            built.
        :param group_factory: An :class:`ElementGroupFactory`.
        :param real_dtype: The :mod:`numpy` data type used for representing real
            data, either :class:`numpy.float32` or :class:`numpy.float64`.
        """

        if not isinstance(actx, ArrayContext):
            raise TypeError("'actx' must be an ArrayContext")

        self.mesh = mesh
        groups = []
        for mg in mesh.groups:
            ng = group_factory(mg, len(groups))
            groups.append(ng)

        self.groups = groups

        self.real_dtype = np.dtype(real_dtype)
        self.complex_dtype = np.dtype({
                np.float32: np.complex64,
                np.float64: np.complex128
                }[self.real_dtype.type])

        self._setup_actx = actx
        self._group_factory = group_factory

    def copy(self, actx=None, mesh=None, group_factory=None, real_dtype=None):
        """Creates a new object of the same type with all arguments that are not
        *None* replaced. The copy is not recursive (e.g. it does not call
        :meth:`meshmode.mesh.Mesh.copy`).
        """

        return type(self)(
                self._setup_actx if actx is None else actx,
                self.mesh if mesh is None else mesh,
                self._group_factory if group_factory is None else group_factory,
                self.real_dtype if real_dtype is None else real_dtype,
                )

    @property
    def dim(self):
        return self.mesh.dim

    @property
    def ambient_dim(self):
        return self.mesh.ambient_dim

    @property
    def ndofs(self):
        return sum(grp.ndofs for grp in self.groups)

    def _new_array(self, actx, creation_func, dtype=None):
        if dtype is None:
            dtype = self.real_dtype
        elif dtype == "c":
            dtype = self.complex_dtype
        else:
            dtype = np.dtype(dtype)

        return _DOFArray(actx, tuple(
            creation_func(shape=(grp.nelements, grp.nunit_dofs), dtype=dtype)
            for grp in self.groups))

    def empty(self, actx: ArrayContext, dtype=None):
        """Return an empty :class:`~meshmode.dof_array.DOFArray`.

        :arg dtype: type special value 'c' will result in a
            vector of dtype :attr:`complex_dtype`. If
            *None* (the default), a real vector will be returned.
        """
        if not isinstance(actx, ArrayContext):
            raise TypeError("'actx' must be an ArrayContext, not '%s'"
                    % type(actx).__name__)

        return self._new_array(actx, actx.empty, dtype=dtype)

    def zeros(self, actx: ArrayContext, dtype=None):
        """Return a zero-initialized :class:`~meshmode.dof_array.DOFArray`.

        :arg dtype: type special value 'c' will result in a
            vector of dtype :attr:`complex_dtype`. If
            *None* (the default), a real vector will be returned.
        """
        if not isinstance(actx, ArrayContext):
            raise TypeError("'actx' must be an ArrayContext, not '%s'"
                    % type(actx).__name__)

        return self._new_array(actx, actx.zeros, dtype=dtype)

    def empty_like(self, array: _DOFArray):
        return self.empty(array.array_context, dtype=array.entry_dtype)

    def zeros_like(self, array: _DOFArray):
        return self.zeros(array.array_context, dtype=array.entry_dtype)

    def num_reference_derivative(self, ref_axes, vec):
        actx = vec.array_context
        ref_axes = list(ref_axes)

        @memoize_in(actx, (Discretization, "reference_derivative_prg"))
        def prg():
            return make_loopy_program(
                """{[iel,idof,j]:
                    0<=iel<nelements and
                    0<=idof,j<nunit_dofs}""",
                "result[iel,idof] = sum(j, diff_mat[idof, j] * vec[iel, j])",
                name="diff")

        def get_mat(grp):
            mat = None
            for ref_axis in ref_axes:
                next_mat = grp.diff_matrices()[ref_axis]
                if mat is None:
                    mat = next_mat
                else:
                    mat = np.dot(next_mat, mat)

            return mat

        return _DOFArray(actx, tuple(
                actx.call_loopy(
                    prg(), diff_mat=actx.from_numpy(get_mat(grp)), vec=vec[grp.index]
                    )["result"]
                for grp in self.groups))

    @memoize_method
    def quad_weights(self):
        """:returns: A :class:`~meshmode.dof_array.DOFArray` with quadrature weights.
        """
        actx = self._setup_actx

        @memoize_in(actx, (Discretization, "quad_weights_prg"))
        def prg():
            return make_loopy_program(
                "{[iel,idof]: 0<=iel<nelements and 0<=idof<nunit_dofs}",
                "result[iel,idof] = weights[idof]",
                name="quad_weights")

        return _DOFArray(None, tuple(
                actx.freeze(
                    actx.call_loopy(
                        prg(),
                        weights=actx.from_numpy(grp.weights),
                        nelements=grp.nelements,
                        )["result"])
                for grp in self.groups))


# For backwards compatibility, we need to be sure we export the
# alias for NodalDiscretization
from meshmode.discretization.nodal import Discretization

# vim: fdm=marker
