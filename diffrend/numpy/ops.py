import numpy as np
from diffrend.numpy.quaternion import Quaternion


def norm_p(u, p=2):
    return np.sqrt(np.sum(np.abs(u) ** p, axis=-1))


def norm_sqr(u):
    return np.sum(u ** 2, axis=-1)


def norm(u):
    return norm_p(u, 2)


def nonzero_divide(x, y):
    denom = np.where(abs(y) > 0, y, np.ones_like(y))
    return x / denom


def normalize(u):
    u = np.array(u)
    norm_u = norm(u)
    return nonzero_divide(u, norm_u[..., np.newaxis])


def axis_angle_matrix(axis, angle):
    return Quaternion(axis=axis, angle=angle).rotation_matrix


def rotate_axis_angle(axis, angle, vec):
    return np.matmul(axis_angle_matrix(axis, angle), vec)


def crossprod_matrix(v):
    x, y, z = v
    return np.array([[0, -z, y],
                     [z, 0, -x],
                     [-y, x, 0]])


def lookat(eye, at, up):
    """Returns a lookat matrix

    :param eye:
    :param at:
    :param up:
    :return:
    """
    if type(eye) is list:
        eye = np.array(eye, dtype=np.float32)
    if type(at) is list:
        at = np.array(at, dtype=np.float32)
    if type(up) is list:
        up = np.array(up, dtype=np.float32)

    if up.size == 4:
        assert up[3] == 0
        up = up[:3]

    if eye.size == 4:
        assert abs(eye[3]) > 0
        eye = eye[:3] / eye[3]

    if at.size == 4:
        assert abs(at[3]) > 0
        at = at[:3] / at[3]

    z = (eye - at)
    z = (z / np.linalg.norm(z, 2))[:3]

    y = up / np.linalg.norm(up, 2)
    x = np.cross(y, z)

    matrix = np.eye(4)
    matrix[:3, :3] = np.stack((x, y, z), axis=1).T
    matrix[:3, 3] = -eye[:3]
    return matrix


def lookat_inv(eye, at, up):
    """Returns the inverse lookat matrix
    :param eye: camera location
    :param at: lookat point
    :param up: up direction
    :return: 4x4 inverse lookat matrix
    """
    if type(eye) is list:
        eye = np.array(eye, dtype=np.float32)
    if type(at) is list:
        at = np.array(at, dtype=np.float32)
    if type(up) is list:
        up = np.array(up, dtype=np.float32)

    if up.size == 4:
        assert up[3] == 0
        up = up[:3]

    z = (eye - at)
    z = (z / np.linalg.norm(z, 2))[:3]

    y = up / np.linalg.norm(up, 2)
    x = np.cross(y, z)

    matrix = np.eye(4)
    matrix[:3, :3] = np.stack((x, y, z), axis=1)
    matrix[:3, 3] = eye[:3] / eye[3]
    return matrix


def perspective_LH_NO(fovy, aspect, near, far):
    """Left-handed camera with all coords mapped to [-1, 1]
    :param fovy:
    :param aspect:
    :param near:
    :param far:
    :return:
    """
    tanHalfFovy = np.tan(fovy / 2.)
    mat_00 = 1 / (aspect * tanHalfFovy)
    mat_11 = 1 / tanHalfFovy
    mat_22 = (near + far) / (far - near)
    mat_23 = -2 * near * far / (far - near)

    return np.array([[mat_00, 0, 0, 0],
                     [0, mat_11, 0, 0],
                     [0, 0, mat_22, mat_23],
                     [0, 0, 1, 0]])


def perspective_RH_NO(fovy, aspect, near, far):
    """Right-handed camera with all coords mapped to [-1, 1]
    :param fovy:
    :param aspect:
    :param near:
    :param far:
    :return:
    """
    tanHalfFovy = np.tan(fovy / 2.)
    mat_00 = 1 / (aspect * tanHalfFovy)
    mat_11 = 1 / tanHalfFovy
    mat_22 = (near + far) / (far - near)
    mat_23 = -2 * near * far / (far - near)

    return np.array([[mat_00, 0, 0, 0],
                     [0, mat_11, 0, 0],
                     [0, 0, -mat_22, mat_23],
                     [0, 0, -1, 0]])


def perspective(fovy, aspect, near, far, type='RH_NO'):
    perspective_fn = {'LH_NO': perspective_LH_NO,
                      'RH_NO': perspective_RH_NO
                      }
    return perspective_fn[type](fovy, aspect, near, far)


def compute_face_normal(obj, unnormalized=False):
    v0 = obj['v'][obj['f'][:, 0]]
    v1 = obj['v'][obj['f'][:, 1]]
    v2 = obj['v'][obj['f'][:, 2]]

    v01 = v1 - v0
    v02 = v2 - v0

    n = np.cross(v01, v02)
    if unnormalized:
        return n
    denom = norm(n)[..., np.newaxis]
    denom[denom == 0] = 1
    return n / denom


def sph2cart(radius, phi, theta):
    """
    :param radius:
    :param phi: azimuth, i.e., angle between x-axis and xy proj of vector r * sin(theta)
    :param theta:  inclination, i.e., angle between vector and z-axis
    :return: [x, y, z]
    """
    sinth = np.sin(theta)
    x = sinth * np.cos(phi) * radius
    y = sinth * np.sin(phi) * radius
    z = np.cos(theta) * radius
    return x, y, z


def cart2sph(x, y, z):
    r = norm(np.array([x, y, z]))
    if r == 0.0:
        theta = 0.0
        phi = 0.0
    else:
        theta = np.arccos(z / r)
        phi = np.arctan2(y, x)
    return r, phi, theta


def sph2cart_vec(u):
    """
    :param u: N x 3 in [radius, azimuth, inclination]
    :return:
    """
    """
    :param radius:
    :param phi: azimuth, i.e., angle between x-axis and xy proj of vector r * sin(theta)
    :param theta:  inclination, i.e., angle between vector and z-axis
    :return: [x, y, z]
    """
    radius, phi, theta = u[..., 0], u[..., 1], u[..., 2]
    sinth = np.sin(theta)
    x = sinth * np.cos(phi) * radius
    y = sinth * np.sin(phi) * radius
    z = np.cos(theta) * radius
    return np.concatenate((x, y, z), axis=-1)


def cart2sph_vec(u):
    pass
