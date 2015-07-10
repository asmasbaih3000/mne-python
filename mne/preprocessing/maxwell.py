# Authors: Mark Wronkiewicz <wronk.mark@gmail.com>
#          Jussi Nurminen <jnu@iki.fi>


# License: BSD (3-clause)

from __future__ import division
from os import path as op
import warnings
import numpy as np
from scipy.linalg import pinv
from scipy.misc import factorial

from .. import pick_types, pick_info
from ..io.constants import FIFF
from ..forward._compute_forward import _concatenate_coils
from ..forward._make_forward import _read_coil_defs, _create_coils


def maxwell_filter(raw, origin=(0, 0, 40), int_order=8, ext_order=3):
    """Apply Maxwell filter to data using spherical harmonics.

    Parameters
    ----------
    raw : instance of mne.io.Raw
        Data to be filtered
    origin : array-like, shape (3,)
        Origin of internal and external multipolar moment space in head coords
        and in millimeters
    int_order : int
        Order of internal component of spherical expansion
    ext_order : int
        Order of external component of spherical expansion

    Returns
    -------
    raw_sss : instance of mne.io.Raw
        The raw data with Maxwell filtering applied

    Notes
    -----
    .. versionadded:: 0.10

    Equation numbers refer to Taulu and Kajola, 2005.

    There are an absurd number of different possible notations for spherical
    coordinates, which confounds the notation for spherical harmonics.  Here,
    we purposefully stay away from shorthand notation in both and use explicit
    terms (like 'azimuth' and 'polar') to avoid confusion.

    This code was adapted and relicensed (with BSD form) with permission from
    Jussi Nurminen.

    References
    ----------
    .. [1] Taulu and Kajola, 2005. "Presentation of electromagnetic
           multichannel data: The signal space separation method".
           http://lib.tkk.fi/Diss/2008/isbn9789512295654/article2.pdf
    """

    # TODO: Exclude 'bads' in multipolar moment calc, add back during
    # reconstruction
    if len(raw.info['bads']) > 0:
        raise RuntimeError('Maxwell filter does not yet handle bad channels.')

    # TODO: Improve logging process to better match Elekta's version
    # Read coil definitions from file
    all_coils, meg_info = _make_coils(raw.info, accurate=True,
                                      elekta_defs=True)

    # Create coil list and pick MEG channels
    picks = [raw.info['ch_names'].index(coil['chname'])
             for coil in all_coils]
    coils = [all_coils[ci] for ci in picks]
    raw.pick_channels(coil['chname'] for coil in coils)

    data, times = raw[picks, :]

    # Magnetometers (with coil_class == 1.0) must be scaled by 100 to improve
    # numerical stability as they have different scales than gradiometers
    coil_scale = np.ones(len(picks))
    coil_scale[np.array([coil['coil_class'] == 1.0 for coil in coils])] = 100.

    # Compute multipolar moment bases
    origin = np.array(origin) / 1000.  # Convert scale from mm to m
    S_in, S_out = _sss_basis(origin, coils, int_order, ext_order)
    S_tot = np.c_[S_in, S_out]

    # Pseudo-inverse of total multipolar moment basis set (Part of Eq. 37)
    pS_tot = pinv(S_tot, cond=1e-15)
    # Compute multipolar moments of (magnetometer scaled) data (Eq. 37)
    mm = np.dot(pS_tot, data * coil_scale[:, np.newaxis])
    # Reconstruct data from internal space (Eq. 38)
    recon = np.dot(S_in, mm[:S_in.shape[1], :])

    # Return reconstructed raw file object
    raw_sss = _update_info(raw.copy(), origin, int_order, ext_order,
                           data.shape[0], mm.shape[0])
    raw_sss._data[:, :] = recon / coil_scale[:, np.newaxis]

    return raw_sss


def _sss_basis(origin, coils, int_order, ext_order):
    """Compute SSS basis for given conditions.

    Parameters
    ----------
    origin : ndarray, shape (3,)
        Origin of the multipolar moment space in millimeters
    coils : list
        List of MEG coils. Each should contain coil information dict. All
        position info must be in the same coordinate frame as 'origin'
    int_order : int
        Order of the internal multipolar moment space
    ext_order : int
        Order of the external multipolar moment space

    Returns
    -------
    bases: tuple, len (2)
        Internal and external basis sets ndarrays with shape
        (n_coils, n_mult_moments)
    """
    r_int_pts, ncoils, wcoils, int_pts = _concatenate_coils(coils)
    n_sens = len(int_pts)
    n_bases = get_num_moments(int_order, ext_order)
    int_lens = np.insert(np.cumsum(int_pts), obj=0, values=0)

    S_in = np.empty((n_sens, (int_order + 1) ** 2 - 1))
    S_out = np.empty((n_sens, (ext_order + 1) ** 2 - 1))
    S_in.fill(np.nan)
    S_out.fill(np.nan)

    # Set all magnetometers (with 'coil_type' == 1.0) to be scaled by 100
    coil_scale = np.ones((len(coils)))
    coil_scale[np.array([coil['coil_class'] == 1.0 for coil in coils])] = 100.

    if n_bases > n_sens:
        raise ValueError('Number of requested bases (%s) exceeds number of '
                         'sensors (%s)' % (str(n_bases), str(n_sens)))

    # Compute position vector between origin and coil integration pts
    cvec_cart = r_int_pts - origin[np.newaxis, :]
    # Convert points to spherical coordinates
    cvec_sph = _cart_to_sph(cvec_cart)

    # Compute internal/external basis vectors (exclude degree 0; L/RHS Eq. 5)
    for spc, g_func, order in zip([S_in, S_out],
                                  [_grad_in_components, _grad_out_components],
                                  [int_order, ext_order]):
        for deg in range(1, order + 1):
            for order in range(-deg, deg + 1):

                # Compute gradient for all integration points
                grads = -1 * g_func(deg, order, cvec_sph[:, 0], cvec_sph[:, 1],
                                    cvec_sph[:, 2])

                # Gradients dotted with integration point normals and weighted
                all_grads = wcoils * np.einsum('ij,ij->i', grads, ncoils)

                # For order and degree, sum over each sensor's integration pts
                for pt_i in range(0, len(int_lens) - 1):
                    int_pts_sum = \
                        np.sum(all_grads[int_lens[pt_i]:int_lens[pt_i + 1]])
                    spc[pt_i, deg ** 2 + deg + order - 1] = int_pts_sum

        # Scale magnetometers and normalize basis vectors to unity magnitude
        spc *= coil_scale[:, np.newaxis]
        spc /= np.sqrt(np.sum(spc * spc, axis=0))[np.newaxis, :]

    return S_in, S_out


def _sph_harmonic(degree, order, az, pol):
    """Evaluate point in specified multipolar moment. Equation 4.

    When using, pay close attention to inputs. Spherical harmonic notation for
    order/degree, and theta/phi are both reversed in original SSS work compared
    to many other sources. Based on 'legendre_associated' by John Burkardt.

    Parameters
    ----------
    degree : int
        Degree of spherical harmonic. (Usually) corresponds to 'l'
    order : int
        Order of spherical harmonic. (Usually) corresponds to 'm'
    az : float
        Azimuthal (longitudinal) spherical coordinate [0, 2*pi]. 0 is aligned
        with x-axis.
    pol : float
        Polar (or colatitudinal) spherical coordinate [0, pi]. 0 is aligned
        with z-axis.

    Returns
    -------
    base : complex float
        The spherical harmonic value at the specified azimuth and polar angles
    """
    from scipy.special import lpmv

    # Error checks
    if np.abs(order) > degree:
        raise ValueError('Absolute value of expansion coefficient must be <= '
                         'degree')
    if (az < -2 * np.pi).any() or (az > 2 * np.pi).any():
        raise ValueError('Azimuth coords must lie in [-2*pi, 2*pi]')
    if(pol < 0).any() or (pol > np.pi).any():
        raise ValueError('Polar coords must lie in [0, pi]')

    #Ensure that polar and azimuth angles are arrays
    azimuth = np.array(az)
    polar = np.array(pol)

    base = np.sqrt((2 * degree + 1) / (4 * np.pi) * factorial(degree - order) /
                   factorial(degree + order)) * \
        lpmv(order, degree, np.cos(polar)) * np.exp(1j * order * azimuth)
    return base


def _alegendre_deriv(degree, order, val):
    """Compute the derivative of the associated Legendre polynomial at a value.

    Parameters
    ----------
    degree : int
        Degree of spherical harmonic. (Usually) corresponds to 'l'
    order : int
        Order of spherical harmonic. (Usually) corresponds to 'm'
    val : float
        Value to evaluate the derivative at

    Returns
    -------
    dPlm
        Associated Legendre function derivative
    """
    from scipy.special import lpmv

    C = 1
    if order < 0:
        order = abs(order)
        C = (-1) ** order * factorial(degree - order) / factorial(degree +
                                                                  order)
    return C * (order * val * lpmv(order, degree, val) + (degree + order) *
                (degree - order + 1) * np.sqrt(1 - val ** 2) *
                lpmv(order - 1, degree, val)) / (1 - val ** 2)


def _grad_in_components(degree, order, rad, az, pol):
    """Compute gradient of internal component of V(r) spherical expansion.

    Internal component has form: Ylm(pol, az) / (rad ** (degree + 1))

    Parameters
    ----------
    degree : int
        Degree of spherical harmonic. (Usually) corresponds to 'l'
    order : int
        Order of spherical harmonic. (Usually) corresponds to 'm'
    rad : ndarray, shape (n_samples,)
        Array of radii
    az : ndarray, shape (n_samples,)
        Array of azimuthal (longitudinal) spherical coordinates [0, 2*pi]. 0 is
        aligned with x-axis.
    pol : ndarray, shape (n_samples,)
        Array of polar (or colatitudinal) spherical coordinates [0, pi]. 0 is
        aligned with z-axis.

    Returns
    -------
    ndarray, shape (n_samples, 3)
        Gradient of the spherical harmonic and vector specified in rectangular
        coordinates
    """
    # Compute gradients for all spherical coordinates (Eq. 6)
    g_rad = -(degree + 1) / rad ** (degree + 2) * _sph_harmonic(degree, order,
                                                                az, pol)

    g_az = 1 / (rad ** (degree + 2) * np.sin(pol)) * 1j * order * \
        _sph_harmonic(degree, order, az, pol)

    g_pol = 1 / rad ** (degree + 2) * np.sqrt((2 * degree + 1) *
                                              factorial(degree - order) /
                                              (4 * np.pi *
                                               factorial(degree + order))) * \
        -np.sin(pol) * _alegendre_deriv(degree, order, np.cos(pol)) * \
        np.exp(1j * order * az)

    # Get real component of vectors, convert to cartesian coords, and return
    real_grads = _get_real_grad(np.c_[g_rad, g_az, g_pol], order)
    return _sph_to_cart_partials(np.c_[rad, az, pol], real_grads)


def _grad_out_components(degree, order, rad, az, pol):
    """Compute gradient of external component of V(r) spherical expansion.

    External component has form: Ylm(azimuth, polar) * (radius ** degree)

    Parameters
    ----------
    degree : int
        Degree of spherical harmonic. (Usually) corresponds to 'l'
    order : int
        Order of spherical harmonic. (Usually) corresponds to 'm'
    rad : ndarray, shape (n_samples,)
        Array of radii
    az : ndarray, shape (n_samples,)
        Array of azimuthal (longitudinal) spherical coordinates [0, 2*pi]. 0 is
        aligned with x-axis.
    pol : ndarray, shape (n_samples,)
        Array of polar (or colatitudinal) spherical coordinates [0, pi]. 0 is
        aligned with z-axis.

    Returns
    -------
    ndarray, shape (n_samples, 3)
        Gradient of the spherical harmonic and vector specified in rectangular
        coordinates
    """
    # Compute gradients for all spherical coordinates (Eq. 7)
    g_rad = degree * rad ** (degree - 1) * _sph_harmonic(degree, order, az,
                                                         pol)

    g_az = rad ** (degree - 1) / np.sin(pol) * 1j * order * \
        _sph_harmonic(degree, order, az, pol)

    g_pol = rad ** (degree - 1) * np.sqrt((2 * degree + 1) *
                                          factorial(degree - order) /
                                          (4 * np.pi *
                                           factorial(degree + order))) * \
        -np.sin(pol) * _alegendre_deriv(degree, order, np.cos(pol)) * \
        np.exp(1j * order * az)

    # Get real component of vectors, convert to cartesian coords, and return
    real_grads = _get_real_grad(np.c_[g_rad, g_az, g_pol], order)
    return _sph_to_cart_partials(np.c_[rad, az, pol], real_grads)


def _get_real_grad(grad_vec_raw, order):
    """Helper function to convert gradient vector to to real basis functions.

    Parameters
    ----------
    grad_vec_raw : ndarray, shape (n_gradients, 3)
        Gradient array with columns for radius, azimuth, polar points
    order : int
        Order (usually 'm') of multipolar moment.

    Returns
    -------
    grad_vec : ndarray, shape (n_gradients, 3)
        Gradient vectors with only real componnet
    """

    if order > 0:
        grad_vec = np.sqrt(2) * np.real(grad_vec_raw)
    elif order < 0:
        grad_vec = np.sqrt(2) * np.imag(grad_vec_raw)
    else:
        grad_vec = grad_vec_raw

    return np.real_if_close(grad_vec)


def get_num_moments(int_order, ext_order):
    """Compute total number of multipolar moments. Equivalent to eq. 32.

    Parameters
    ----------
    int_order : int
        Internal expansion order
    ext_order : int
        External expansion order

    Returns
    -------
    M : int
        Total number of multipolar moments
    """

    # TODO: Eventually, reuse code in field_interpolation

    M = int_order ** 2 + 2 * int_order + ext_order ** 2 + 2 * ext_order
    return M


def _sph_to_cart_partials(sph_pts, sph_grads):
    """Convert spherical partial derivatives to cartesian coords.

    Note: Because we are dealing with partial derivatives, this calculation is
    not a static transformation. The transformation matrix itself is dependent
    on azimuth and polar coord.

    See the 'Spherical coordinate sytem' section here:
    wikipedia.org/wiki/Vector_fields_in_cylindrical_and_spherical_coordinates

    Parameters
    ----------
    sph_pts : ndarray, shape (n_points, 3)
        Array containing spherical coordinates points (rad, azimuth, polar)
    sph_grads : ndarray, shape (n_points, 3)
        Array containing partial derivatives at each spherical coordinate

    Returns
    -------
    cart_grads : ndarray, shape (n_points, 3)
        Array containing partial derivatives in Cartesian coordinates (x, y, z)
    """

    cart_grads = np.zeros_like(sph_grads)

    # TODO: needs vectorization, currently matching Jussi's code for debugging
    for pt_i, (sph_pt, sph_grad) in enumerate(zip(sph_pts, sph_grads)):
        # Calculate cosine and sine of azimuth and polar coord
        c_a, s_a = np.cos(sph_pt[1]), np.sin(sph_pt[1])
        c_p, s_p = np.cos(sph_pt[2]), np.sin(sph_pt[2])

        trans = np.array([[c_a * s_p, -s_a, c_a * c_p],
                          [s_a * s_p, c_a, c_p * s_a],
                          [c_p, 0, -s_p]])

        cart_grads[pt_i, :] = np.dot(trans, sph_grad)

    return cart_grads


def _cart_to_sph(cart_pts):
    """Convert Cartesian coordinates to spherical coordinates.

    Parameters
    ----------
    cart_pts : ndarray, shape (n_points, 3)
        Array containing points in Cartesian coordinates (x, y, z)

    Returns
    -------
    ndarray, shape (n_points, 3)
        Array containing points in spherical coordinates (rad, azimuth, polar)
    """

    rad = np.linalg.norm(cart_pts, axis=1)
    az = np.arctan2(cart_pts[:, 1], cart_pts[:, 0])
    pol = np.arccos(cart_pts[:, 2] / rad)

    return np.c_[rad, az, pol]


# TODO: Eventually refactor this in forward computation code

def _make_coils(info, accurate=True, elekta_defs=False):
    """Prepare dict of MEG coils and their information.

    Parameters
    ----------
    info : instance of mne.io.meas_info.Info | str
        If str, then it should be a filename to a Raw, Epochs, or Evoked
        file with measurement information. If dict, should be an info
        dict (such as one from Raw, Epochs, or Evoked).
    accurate : bool
        Accuracy of coil information.
    coil_def : str | None
        Filepath to the coil definitions file.

    Returns
    -------
    megcoils, meg_info : dict
        MEG coils and information dict
    """

    if accurate:
        accuracy = FIFF.FWD_COIL_ACCURACY_ACCURATE
    else:
        accuracy = FIFF.FWD_COIL_ACCURACY_NORMAL
    meg_info = None
    megnames, megcoils, compcoils = [], [], []

    # MEG channels
    picks = pick_types(info, meg=True, eeg=False, ref_meg=False,
                       exclude=[])
    nmeg = len(picks)
    if nmeg > 0:
        megchs = pick_info(info, picks)['chs']

    if nmeg <= 0:
        raise RuntimeError('Could not find any MEG channels')

    meg_info = pick_info(info, picks) if nmeg > 0 else None

    # Create coil descriptions with transformation to head or MRI frame
    if elekta_defs:
        elekta_coil_defs = op.join(op.split(__file__)[0], '..', 'data',
                                   'coil_def_Elekta.dat')
        templates = _read_coil_defs(elekta_coil_defs)

        # Check that we have all coils needed
        template_set = set([coil['coil_type'] for coil in templates['coils']])
        req_coil_set = set([coil['coil_type'] for coil in meg_info['chs']])
        if not req_coil_set.issubset(template_set):
            warnings.warn('Didn\'t locate find enough Elekta coil definitions,'
                          ' using default MNE coils.')
            templates = _read_coil_defs()
    else:
        templates = _read_coil_defs()

    if nmeg > 0:
        # TODO: In fwd solution code, reformulate check that forces head
        # coords and remove this hack. (Or use only head coords)
        #info['dev_head_t']['trans'] = np.eye(4)  # Uncomment for device coords
        megcoils = _create_coils(megchs, accuracy, info['dev_head_t'], 'meg',
                                 templates)

    return megcoils, meg_info


def _update_info(raw, origin, int_order, ext_order, nsens, nmoments):
    """Helper function to update info after Maxwell filtering."""

    info_dict = dict(int_order=int_order, ext_order=ext_order,
                     origin=origin, nsens=nsens, nmoments=nmoments,
                     creator='MNE\'s Maxwell Filter')
    raw.info['maxshield'] = False

    # Insert information in raw.info['proc_info']
    if 'proc_history' in raw.info.keys():
        raw.info['proc_history'].insert(0, info_dict)
    else:
        raw.info['proc_history'] = [info_dict]

    return raw
