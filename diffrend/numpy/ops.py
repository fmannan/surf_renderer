import numpy as np
from diffrend.numpy.quaternion import Quaternion


def norm_p(u, p=2):
    return np.sqrt(np.sum(np.abs(u) ** p, axis=-1))


def norm_sqr(u):
    return np.sum(u ** 2, axis=-1)


def norm(u):
    return np.sqrt(norm_sqr(u))


def normalize(u):
    u = np.array(u)
    return u / norm(u)


def rotate_angle_axis(angle, axis, vec):
    pass


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

    z = (eye - at)
    z = (z / np.linalg.norm(z, 2))[:3]

    y = up / np.linalg.norm(up, 2)
    x = np.cross(y, z)

    matrix = np.eye(4)
    matrix[:3, :3] = np.stack((x, y, z), axis=1).T
    matrix[:3, 3] = -eye[:3] / eye[3]
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