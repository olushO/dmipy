import numpy as np
from dipy.data import get_sphere
from dipy.reconst.shm import sh_to_sf_matrix
from ..utils.utils import (
    unitsphere2cart_Nd, T1_tortuosity, fractional_parameter)
from ..utils.spherical_mean import (
    estimate_spherical_mean_multi_shell)
from functools import partial


__all__ = [
    'FittedMultiCompartmentModel',
    'FittedMultiCompartmentSphericalMeanModel'
]


class FittedMultiCompartmentModel:
    """
    The FittedMultiCompartmentModel instance contains information about the
    original MultiCompartmentModel, the estimated S0 values, the fitting mask
    and the fitted model parameters.

    Parameters
    ----------
    model : MultiCompartmentModel instance,
        A dmipy MultiCompartmentModel.
    S0 : array of size (Ndata,) or (N_data, N_DWIs),
        Array containing the estimated S0 values of the data. If data is 4D,
        then S0 is 3D if there is only one TE, and the same 4D size of the data
        if there are multiple TEs.
    mask : array of size (N_data,),
        boolean mask of voxels that were fitted.
    fitted_parameters_vector : array of size (N_data, Nparameters),
        fitted model parameters array.
    """

    def __init__(self, model, S0, mask, fitted_parameters_vector):
        self.model = model
        self.S0 = S0
        self.mask = mask
        self.fitted_parameters_vector = fitted_parameters_vector

    @property
    def fitted_parameters(self):
        "Returns the fitted parameters as a dictionary."
        return self.model.parameter_vector_to_parameters(
            self.fitted_parameters_vector)

    @property
    def fitted_and_linked_parameters(self):
        "Returns the fitted and linked parameters as a dictionary."
        fitted_parameters = self.model.parameter_vector_to_parameters(
            self.fitted_parameters_vector)
        return self.model.add_linked_parameters_to_parameters(
            fitted_parameters)

    def fod(self, vertices, visual_odi_lower_bound=0.):
        """
        Returns the Fiber Orientation Distribution if it is available.

        Parameters
        ----------
        vertices : array of size (Nvertices, 3),
            Array of cartesian unit vectors at which to sample the FOD.
        visual_odi_lower_bound : float,
            gives a lower bound to the Orientation Distribution Index (ODI) of
            FODs of Watson and Bingham distributions. This can be useful to
            visualize FOD fields where some FODs are extremely sharp.

        Returns
        -------
        fods : array of size (Ndata, Nvertices),
            the FODs of the fitted model, scaled by volume fraction.
        """
        if not self.model.fod_available:
            msg = ('FODs not available for current model.')
            raise ValueError(msg)
        dataset_shape = self.fitted_parameters_vector.shape[:-1]
        N_samples = len(vertices)
        fods = np.zeros(np.r_[dataset_shape, N_samples])
        mask_pos = np.where(self.mask)
        for pos in zip(*mask_pos):
            parameters = self.model.parameter_vector_to_parameters(
                self.fitted_parameters_vector[pos])
            if visual_odi_lower_bound > 0:
                param_keys = parameters.keys()
                for key in param_keys:
                    if key[-3:] == 'odi':
                        parameters[key] = np.clip(parameters[key],
                                                  visual_odi_lower_bound, 1)
            fods[pos] = self.model(vertices, quantity='FOD', **parameters)
        return fods

    def fod_sh(self, sh_order=8, basis_type=None):
        """
        Returns the spherical harmonics coefficients of the Fiber Orientation
        Distribution (FOD) if it is available. Uses are 724 spherical
        tessellation to do the spherical harmonics transform.

        Parameters
        ----------
        sh_order : integer,
            the maximum spherical harmonics order of the coefficient expansion.
        basis_type : string,
            type of spherical harmonics basis to use for the expansion, see
            sh_to_sf_matrix for more info.

        Returns
        -------
        fods_sh : array of size (Ndata, Ncoefficients),
            spherical harmonics coefficients of the FODs, scaled by volume
            fraction.
        """
        if not self.model.fod_available:
            msg = ('FODs not available for current model.')
            raise ValueError(msg)
        sphere = get_sphere(name='repulsion724')
        vertices = sphere.vertices
        _, inv_sh_matrix = sh_to_sf_matrix(
            sphere, sh_order, basis_type=basis_type, return_inv=True)
        fods_sf = self.fod(vertices)

        dataset_shape = self.fitted_parameters_vector.shape[:-1]
        number_coef_used = int((sh_order + 2) * (sh_order + 1) // 2)
        fods_sh = np.zeros(np.r_[dataset_shape, number_coef_used])
        mask_pos = np.where(self.mask)
        for pos in zip(*mask_pos):
            fods_sh[pos] = np.dot(inv_sh_matrix.T, fods_sf[pos])
        return fods_sh

    def peaks_spherical(self):
        "Returns the peak angles of the model."
        mu_params = []
        for name, card in self.model.parameter_cardinality.items():
            if name[-2:] == 'mu' and card == 2:
                mu_params.append(self.fitted_parameters[name])
        if len(mu_params) == 0:
            msg = ('peaks not available for current model.')
            raise ValueError(msg)
        if len(mu_params) == 1:
            return np.expand_dims(mu_params[0], axis=-2)
        return np.concatenate([mu[..., None] for mu in mu_params], axis=-1)

    def peaks_cartesian(self):
        "Returns the cartesian peak unit vectors of the model."
        peaks_spherical = self.peaks_spherical()
        peaks_cartesian = unitsphere2cart_Nd(peaks_spherical)
        return peaks_cartesian

    def predict(self, acquisition_scheme=None, S0=None, mask=None):
        """
        simulates the dMRI signal of the fitted MultiCompartmentModel for the
        estimated model parameters. If no acquisition_scheme is given, then
        the same acquisition_scheme that was used for the fitting is used. If
        no S0 is given then it is assumed to be the estimated one. If no mask
        is given then all voxels are assumed to have been fitted.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy.
        S0 : None or float,
            Signal intensity without diffusion sensitization. If None, uses
            estimated SO from fitting process. If float, uses that value.
        mask : (N-1)-dimensional integer/boolean array of size (N_x, N_y, ...),
            mask of voxels to simulate data at.

        Returns
        -------
        predicted_signal : array of size (Ndata, N_DWIS),
            predicted DWIs for the given model parameters and acquisition
            scheme.
        """
        if acquisition_scheme is None:
            acquisition_scheme = self.model.scheme
        dataset_shape = self.fitted_parameters_vector.shape[:-1]
        if S0 is None:
            S0 = self.S0
        elif isinstance(S0, float):
            S0 = np.ones(dataset_shape) * S0
        if mask is None:
            mask = self.mask

        N_samples = len(acquisition_scheme.bvalues)

        predicted_signal = np.zeros(np.r_[dataset_shape, N_samples])
        mask_pos = np.where(mask)
        for pos in zip(*mask_pos):
            parameters = self.model.parameter_vector_to_parameters(
                self.fitted_parameters_vector[pos])
            predicted_signal[pos] = self.model(
                acquisition_scheme, **parameters) * S0[pos]
        return predicted_signal

    def R2_coefficient_of_determination(self, data):
        "Calculates the R-squared of the model fit."
        data_ = data / self.S0[..., None]

        y_hat = self.predict(S0=1.)
        y_bar = np.mean(data_, axis=-1)
        SStot = np.sum((data_ - y_bar[..., None]) ** 2, axis=-1)
        SSres = np.sum((data_ - y_hat) ** 2, axis=-1)
        R2 = 1 - SSres / SStot
        R2[~self.mask] = 0
        return R2

    def mean_squared_error(self, data):
        "Calculates the mean squared error of the model fit."
        if self.model.scheme.TE is None:
            data_ = data / self.S0[..., None]
        else:
            data_ = data / self.S0

        y_hat = self.predict(S0=1.)
        mse = np.mean((data_ - y_hat) ** 2, axis=-1)
        mse[~self.mask] = 0
        return mse


class FittedMultiCompartmentSphericalMeanModel:
    """
    The FittedMultiCompartmentModel instance contains information about the
    original MultiCompartmentModel, the estimated S0 values, the fitting mask
    and the fitted model parameters.

    Parameters
    ----------
    model : MultiCompartmentModel instance,
        A dmipy MultiCompartmentModel.
    S0 : array of size (Ndata,) or (N_data, N_DWIs),
        Array containing the estimated S0 values of the data. If data is 4D,
        then S0 is 3D if there is only one TE, and the same 4D size of the data
        if there are multiple TEs.
    mask : array of size (N_data,),
        boolean mask of voxels that were fitted.
    fitted_parameters_vector : array of size (N_data, Nparameters),
        fitted model parameters array.
    """

    def __init__(self, model, S0, mask, fitted_parameters_vector):
        self.model = model
        self.S0 = S0
        self.mask = mask
        self.fitted_parameters_vector = fitted_parameters_vector

    @property
    def fitted_parameters(self):
        "Returns the fitted parameters as a dictionary."
        return self.model.parameter_vector_to_parameters(
            self.fitted_parameters_vector)

    @property
    def fitted_and_linked_parameters(self):
        "Returns the fitted and linked parameters as a dictionary."
        fitted_parameters = self.model.parameter_vector_to_parameters(
            self.fitted_parameters_vector)
        return self.model.add_linked_parameters_to_parameters(
            fitted_parameters)

    def predict(self, acquisition_scheme=None, S0=None, mask=None):
        """
        simulates the dMRI signal of the fitted MultiCompartmentModel for the
        estimated model parameters. If no acquisition_scheme is given, then
        the same acquisition_scheme that was used for the fitting is used. If
        no S0 is given then it is assumed to be the estimated one. If no mask
        is given then all voxels are assumed to have been fitted.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy.
        S0 : None or float,
            Signal intensity without diffusion sensitization. If None, uses
            estimated SO from fitting process. If float, uses that value.
        mask : (N-1)-dimensional integer/boolean array of size (N_x, N_y, ...),
            mask of voxels to simulate data at.

        Returns
        -------
        predicted_signal : array of size (Ndata, N_DWIS),
            predicted DWIs for the given model parameters and acquisition
            scheme.
        """
        if acquisition_scheme is None:
            acquisition_scheme = self.model.scheme
        dataset_shape = self.fitted_parameters_vector.shape[:-1]
        if S0 is None:
            S0 = self.S0
        elif isinstance(S0, float):
            S0 = np.ones(dataset_shape) * S0
        if mask is None:
            mask = self.mask

        N_samples = len(acquisition_scheme.shell_bvalues)

        predicted_signal = np.zeros(np.r_[dataset_shape, N_samples])
        mask_pos = np.where(mask)
        for pos in zip(*mask_pos):
            parameters = self.model.parameter_vector_to_parameters(
                self.fitted_parameters_vector[pos])
            predicted_signal[pos] = self.model(
                acquisition_scheme, **parameters) * S0[pos]
        return predicted_signal

    def R2_coefficient_of_determination(self, data):
        "Calculates the R-squared of the model fit."
        Nshells = len(self.model.scheme.shell_bvalues)
        data_ = np.zeros(np.r_[data.shape[:-1], Nshells])
        for pos in zip(*np.where(self.mask)):
            data_[pos] = estimate_spherical_mean_multi_shell(
                data[pos] / self.S0[pos], self.model.scheme)

        y_hat = self.predict(S0=1.)
        y_bar = np.mean(data_, axis=-1)
        SStot = np.sum((data_ - y_bar[..., None]) ** 2, axis=-1)
        SSres = np.sum((data_ - y_hat) ** 2, axis=-1)
        R2 = 1 - SSres / SStot
        R2[~self.mask] = 0
        return R2

    def mean_squared_error(self, data):
        "Calculates the mean squared error of the model fit."
        Nshells = len(self.model.scheme.shell_bvalues)
        data_ = np.zeros(np.r_[data.shape[:-1], Nshells])
        for pos in zip(*np.where(self.mask)):
            data_[pos] = estimate_spherical_mean_multi_shell(
                data[pos] / self.S0[pos], self.model.scheme)

        y_hat = self.predict(S0=1.)
        mse = np.mean((data_ - y_hat) ** 2, axis=-1)
        mse[~self.mask] = 0
        return mse

    def return_parametric_fod_model(
            self, distribution='watson', Ncompartments=1):
        """
        Retuns parametric FOD model using the rotational harmonics of the
        fitted spherical mean model as the convolution kernel. It can be called
        with any implemented parametric distribution (Watson/Bingham) and for
        any number of compartments.

        Internally, the input models to the spherical mean model are given to
        a spherically distributed model where the parameter links are replayed
        such that the distributed model has the same parameter constraints as
        the spherical mean model. This distributed model now represents one
        compartment of "bundle". This bundle representation is copied
        Ncompartment times and given as input to a MultiCompartmentModel, where
        now the non-linear are all linked such that each bundle has the same
        convolution kernel. Finally, the FittedSphericalMeanModel parameters
        are given as fixed parameters for the kernel (the kernel will not be
        fitted while the FOD's distribution parameters are being optimized).

        The function returns a MultiCompartmentModel instance that can be
        interacted with as usual to fit dMRI data.

        Parameters
        ----------
        distribution: string,
            Choice of parametric spherical distribution.
            Can be 'watson', or 'bingham'.
        Ncompartments: integer,
            Number of bundles that will be fitted. Must be larger than zero.

        Returns
        -------
        mc_bundles_model: Dmipy MultiCompartmentModel instance,
            MultiCompartmentModel instance that can be used to estimate
            parametric FODs using the fitted spherical mean model as a kernel.
        """
        from .modeling_framework import MultiCompartmentModel
        from ..distributions import distribute_models

        if not isinstance(Ncompartments, int) or Ncompartments < 1:
            msg = 'Ncompartments must be integer larger or equal to one.'
            raise ValueError(msg)

        if distribution is 'watson':
            bundle = distribute_models.SD1WatsonDistributed(
                self.model.models)
            basename = 'SD1WatsonDistributed_'
        elif distribution is 'bingham':
            bundle = distribute_models.SD2BinghamDistributed(
                self.model.models)
            basename = 'SD2BinghamDistributed_'
        else:
            msg = '{} is not a valid distribution choice'.format(
                distribute_models)
            raise ValueError(msg)

        for link in self.model.parameter_links:
            param_to_delete = self.model._inverted_parameter_map[link[0],
                                                                 link[1]]
            if link[2] is T1_tortuosity:
                bundle.parameter_links.append(
                    [link[0], link[1], link[2], link[3][:-1]])
            elif link[2] is fractional_parameter:
                new_parameter_name = param_to_delete + '_fraction'
                bundle.parameter_ranges.update({new_parameter_name: [0., 1.]})
                bundle.parameter_scales.update({new_parameter_name: 1.})
                bundle.parameter_cardinality.update({new_parameter_name: 1})
                bundle.parameter_types.update({new_parameter_name: 'normal'})

                bundle._parameter_map.update(
                    {new_parameter_name: (None, 'fraction')})
                bundle._inverted_parameter_map.update(
                    {(None, 'fraction'): new_parameter_name})

                # add parmeter link to fractional parameter
                param_larger_than = self.model._inverted_parameter_map[
                    link[3][1][0], link[3][1][1]]

                model, name = bundle._parameter_map[param_to_delete]
                bundle.parameter_links.append(
                    [model, name, fractional_parameter, [
                        bundle._parameter_map[new_parameter_name],
                        bundle._parameter_map[param_larger_than]]])
            else:
                bundle.parameter_links.append(link)
            del bundle.parameter_ranges[param_to_delete]
            del bundle.parameter_cardinality[param_to_delete]
            del bundle.parameter_scales[param_to_delete]
            del bundle.parameter_types[param_to_delete]

        bundles = [bundle.copy() for i in range(Ncompartments)]
        mc_bundles_model = MultiCompartmentModel(bundles)
        parameter_pairs = []
        for smt_par_name in self.model.parameter_names:
            parameters = []
            parameters.append(smt_par_name)
            for mc_par_name in mc_bundles_model.parameter_names:
                if (mc_par_name.startswith(basename) and
                        mc_par_name.endswith(smt_par_name)):
                    parameters.append(mc_par_name)
            if len(parameters) > 1:
                parameter_pairs.append(parameters)

        for parameters in parameter_pairs:
            for i in range(2, Ncompartments + 1):
                mc_bundles_model.set_equal_parameter(parameters[1],
                                                     parameters[i])

        for parameters in parameter_pairs:
            smt_parameter_name = parameters[0]
            mc_parameter_name = parameters[1]
            mc_bundles_model.set_fixed_parameter(
                mc_parameter_name,
                self.fitted_parameters[smt_parameter_name])
        return mc_bundles_model
