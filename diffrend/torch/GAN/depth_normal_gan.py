"""Generator."""
from __future__ import absolute_import

import copy
import numpy as np
from scipy.misc import imsave
import os
import sys
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.optim as optim
import torch.utils.data
from torch.autograd import Variable
import torch.nn.functional as F
from imageio import imsave
import shutil
import torchvision
from diffrend.torch.GAN.datasets import Dataset_load
from diffrend.torch.GAN.twin_networks import create_networks
from diffrend.torch.GAN.parameters_halfbox_shapenet import Parameters
from diffrend.torch.params import SCENE_SPHERE_HALFBOX_0
from diffrend.torch.utils import ( where)
from diffrend.torch.utils import (tch_var_f, tch_var_l, CUDA, get_data, get_normalmap_image,
                                  world_to_cam, cam_to_world, normalize, unit_norm2_L2loss,
                                  normalize_maxmin, normal_consistency_cost, away_from_camera_penalty,
                                  spatial_3x3, depth_rgb_gradient_consistency, grad_spatial2d)
from diffrend.torch.renderer import (render, render_splats_NDC,
                                     render_splats_along_ray, z_to_pcl_CC)
from diffrend.torch.NEstNet import NEstNet_v0
from diffrend.utils.sample_generator import uniform_sample_sphere
from diffrend.torch.ops import sph2cart_unit
from diffrend.utils.utils import contrast_stretch_percentile, save_xyz

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
from mpl_toolkits.mplot3d import axes3d
from diffrend.torch.utils import get_normalmap_image
# try: # temporarily
#     from hyperdash import Experiment
#     HYPERDASH_SUPPORTED = True
# except ImportError:
HYPERDASH_SUPPORTED = False

"""
z_noise -> z_distance_generator -> proj_to_scene_in_CC -> normal_estimator_network -> render -> discriminator
real_data -> discriminator
"""


def copy_scripts_to_folder(expr_dir):
    """Copy scripts."""
    shutil.copy("twin_networks.py", expr_dir)
    shutil.copy("../NEstNet.py", expr_dir)
    shutil.copy("../params.py", expr_dir)
    shutil.copy("../renderer.py", expr_dir)
    shutil.copy("objects_folder_multi.py", expr_dir)
    shutil.copy("parameters_halfbox_shapenet.py", expr_dir)
    shutil.copy(__file__, expr_dir)


def mkdirs(paths):
    """Create paths."""
    if isinstance(paths, list) and not isinstance(paths, str):
        for path in paths:
            mkdir(path)
    else:
        mkdir(paths)


def mkdir(path):
    """Create a directory."""
    if not os.path.exists(path):
        os.makedirs(path)


def create_scene(width, height, fovy, focal_length, n_samples):
    """Create a semi-empty scene with camera parameters."""
    # Create a splats rendering scene
    scene = copy.deepcopy(SCENE_SPHERE_HALFBOX_0)

    # Define the camera parameters
    scene['camera']['viewport'] = [0, 0, width, height]
    scene['camera']['fovy'] = np.deg2rad(fovy)
    scene['camera']['focal_length'] = focal_length

    return scene


def calc_gradient_penalty(discriminator, real_data, fake_data, fake_data_cond,
                          gp_lambda):
    """Calculate GP."""
    assert real_data.size(0) == fake_data.size(0)
    alpha = torch.rand(real_data.size(0), 1, 1, 1)
    alpha = alpha.expand(real_data.size())
    alpha = alpha.cuda()

    interpolates = Variable(alpha * real_data + ((1 - alpha) * fake_data),
                            requires_grad=True)
    interpolates_cond = Variable(fake_data_cond, requires_grad=True)
    disc_interpolates = discriminator(interpolates, interpolates_cond)
    gradients = torch.autograd.grad(
        outputs=disc_interpolates, inputs=interpolates,
        grad_outputs=torch.ones(disc_interpolates.size()).cuda(),
        create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradients = gradients.contiguous().view(gradients.size(0), -1)
    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean() * gp_lambda

    return gradient_penalty


class GAN(object):
    """GAN class."""

    def __init__(self, opt, dataset_load=None, experiment=None, exp_dir=None):
        """Constructor."""
        # Save variables
        self.opt = opt
        self.exp = experiment
        self.real_label = 1
        self.fake_label = 0
        self.dataset_load = dataset_load
        self.opt.out_dir = exp_dir
        file_name = os.path.join(self.opt.out_dir, 'losses.txt')
        self.output_loss_file=open(file_name, "wt")
        if self.opt.full_sphere_sampling:
            self.opt.phi = None
            self.opt.theta = None
            self.opt.cam_dist = self.opt.cam_dist + 0.2

        else:
            self.opt.angle = None
            self.opt.axis=None
        # Create dataset loader
        self.create_dataset_loader()

        # Create the networks
        self.create_networks()

        # Create create_tensors
        self.create_tensors()

        # Create criterion
        self.create_criterion()

        # Create create optimizers
        self.create_optimizers()

        # Create splats rendering scene
        self.create_scene()

    def create_dataset_loader(self, ):
        """Create dataset leader."""
        # Define camera positions
        if self.opt.same_view:
            if not self.opt.toy_example:
                self.cam_pos = uniform_sample_sphere(radius=self.opt.cam_dist,
                                                     num_samples=1)
            else:
                arrays = [np.asarray([3., 3., 3.]) for _ in range(self.opt.batchSize)]
                self.cam_pos = np.stack(arrays, axis=0)

        # Create dataset loader
        if not self.opt.toy_example:
            self.dataset_load.initialize_dataset()
            self.dataset = self.dataset_load.get_dataset()
            self.dataset_load.initialize_dataset_loader(1)  # TODO: Hack
            self.dataset_loader = self.dataset_load.get_dataset_loader()

    def create_networks(self, ):
        """Create networks."""
        self.netG, self.netG2, self.netD, self.netD2 = create_networks(
            self.opt, verbose=True, depth_only=True)
        assert self.netG2 is None
        # Create the normal estimation network which takes pointclouds in the camera space
        # and outputs the normals
        self.sph_normals = True
        self.netG2 = NEstNet_v0(sph=self.sph_normals)
        print(self.netG2)
        if not self.opt.no_cuda:
            self.netD = self.netD.cuda()
            self.netG = self.netG.cuda()
            self.netG2 = self.netG2.cuda()

    def create_scene(self, ):
        """Create a semi-empty scene with camera parameters."""
        self.scene = create_scene(
            self.opt.splats_img_size, self.opt.splats_img_size, self.opt.fovy,
            self.opt.focal_length, self.opt.n_splats)

    def create_tensors(self, ):
        """Create the tensors."""
        # Create tensors
        self.input = torch.FloatTensor(
            self.opt.batchSize, self.opt.render_img_nc,
            self.opt.render_img_size, self.opt.render_img_size)

        self.input_depth = torch.FloatTensor(
            self.opt.batchSize, 1, self.opt.render_img_size,
            self.opt.render_img_size)
        self.input_normal = torch.FloatTensor(
            self.opt.batchSize, 1, self.opt.render_img_size,
            self.opt.render_img_size)
        self.input_cond = torch.FloatTensor(self.opt.batchSize, 3)

        self.noise = torch.FloatTensor(
            self.opt.batchSize, int(self.opt.nz), 1, 1)

        self.fixed_noise = torch.FloatTensor(
            self.opt.batchSize, int(self.opt.nz), 1, 1).normal_(0, 1)

        self.label = torch.FloatTensor(2*self.opt.batchSize)
        self.one = torch.FloatTensor([1])
        self.mone = self.one * -1

        # Move them to the GPU
        if not self.opt.no_cuda:
            self.input = self.input.cuda()

            self.input_depth = self.input_depth.cuda()
            self.input_normal = self.input_normal.cuda()
            self.input_cond = self.input_cond.cuda()

            self.label = self.label.cuda()
            self.noise = self.noise.cuda()
            self.fixed_noise = self.fixed_noise.cuda()


            self.one = self.one.cuda()
            self.mone = self.mone.cuda()

        self.fixed_noise = Variable(self.fixed_noise)


    def create_criterion(self, ):
        """Create criterion."""
        self.criterion = nn.BCELoss()
        if not self.opt.no_cuda:
            self.criterion = self.criterion.cuda()

    def create_optimizers(self, ):
        """Create optimizers."""
        if self.opt.optimizer == 'adam':
            self.optimizerD = optim.Adam(self.netD.parameters(),
                                         lr=self.opt.lr,
                                         betas=(self.opt.beta1, 0.999))

            self.optimizerG = optim.Adam(self.netG.parameters(),
                                         lr=self.opt.lr,
                                         betas=(self.opt.beta1, 0.999))
            self.optimizerG2 = optim.Adam(self.netG2.parameters(),
                                          lr=self.opt.lr,
                                          betas=(self.opt.beta1, 0.999))
        elif self.opt.optimizer == 'rmsprop':
            self.optimizerD = optim.RMSprop(self.netD.parameters(),
                                            lr=self.opt.lr)

            self.optimizerG = optim.RMSprop(self.netG.parameters(),
                                            lr=self.opt.lr)
            self.optimizerG2 = optim.RMSprop(self.netG2.parameters(),
                                             lr=self.opt.lr)
        else:
            raise ValueError('Unknown optimizer: ' + self.opt.optimizer)

        # Create the schedulers
        if self.opt.lr_sched_type == 'step':
            LR_fn = optim.lr_scheduler.StepLR
        elif self.opt.lr_sched_type == 'exp':
            LR_fn = optim.lr_scheduler.ExponentialLR
        elif self.opt.lr_sched_type is None:
            LR_fn = None
        else:
            raise ValueError('Unknown scheduler')

        self.optG_z_lr_scheduler = LR_fn(
            self.optimizerG, step_size=self.opt.z_lr_sched_step,
            gamma=self.opt.z_lr_sched_gamma)
        self.optG2_normal_lr_scheduler = LR_fn(
            self.optimizerG2, step_size=self.opt.normal_lr_sched_step,
            gamma=self.opt.normal_lr_sched_gamma)

        self.LR_SCHED_MAP = [self.optG_z_lr_scheduler,
                             self.optG2_normal_lr_scheduler]
        self.OPT_MAP = [self.optimizerG, self.optimizerG2]

    def get_samples(self):
        """Get samples."""
        if not self.opt.toy_example:
            # Load a batch of samples
            try:
                samples = self.data_iter.next()
            except StopIteration:
                del self.data_iter
                self.data_iter = iter(self.dataset_loader)
                samples = self.data_iter.next()
            except AttributeError:
                self.data_iter = iter(self.dataset_loader)
                samples = self.data_iter.next()
        # else:
        #     samples = self.generate_toy_samples()

        return samples

    def get_real_samples(self):
        """Get a real sample."""
        # samples = self.get_samples()

        # Define the camera poses
        if not self.opt.same_view:
            if self.opt.full_sphere_sampling:
                self.cam_pos = uniform_sample_sphere(
                    radius=self.opt.cam_dist, num_samples=self.opt.batchSize,
                    axis=self.opt.axis, angle=np.deg2rad(self.opt.angle),
                    theta_range=self.opt.theta, phi_range=self.opt.phi)
            else:
                self.cam_pos = uniform_sample_sphere(
                    radius=self.opt.cam_dist, num_samples=self.opt.batchSize,
                    axis=self.opt.axis, angle=self.opt.angle,
                    theta_range=np.deg2rad(self.opt.theta), phi_range=np.deg2rad(self.opt.phi))



        # Create a splats rendering scene
        large_scene = create_scene(self.opt.width, self.opt.height,
                                   self.opt.fovy, self.opt.focal_length,
                                   self.opt.n_splats)
        lookat = self.opt.at if self.opt.at is not None else [0.0, 0.0, 0.0, 1.0]
        large_scene['camera']['at'] = tch_var_f(lookat)

        # Render scenes
        data, data_depth, data_normal, data_cond = [], [], [], []
        inpath = self.opt.out_dir + '/'
        for idx in range(self.opt.batchSize):
            # Save the splats into the rendering scene
            if not self.opt.toy_example:
                if self.opt.use_mesh:
                    if 'sphere' in large_scene['objects']:
                        del large_scene['objects']['sphere']
                    if 'disk' in large_scene['objects']:
                        del large_scene['objects']['disk']
                    if 'triangle' not in large_scene['objects']:
                        large_scene['objects'] = {
                            'triangle': {'face': None, 'normal': None,
                                         'material_idx': None}}
                    samples = self.get_samples()

                    # # TODO: Solve this hack!!!!!!
                    # while True:
                    #     samples = self.get_samples()
                    #     if samples['mesh']['face'][0].size(0) <= 3000:
                    #         break
                    # print (samples['mesh']['face'][0].size())
                    large_scene['objects']['triangle']['material_idx'] = tch_var_l(
                        np.zeros(samples['mesh']['face'][0].shape[0], dtype=int).tolist())
                    large_scene['objects']['triangle']['face'] = Variable(
                        samples['mesh']['face'][0].cuda(), requires_grad=False)
                    large_scene['objects']['triangle']['normal'] = Variable(
                        samples['mesh']['normal'][0].cuda(),
                        requires_grad=False)
                else:
                    if 'sphere' in large_scene['objects']:
                        del large_scene['objects']['sphere']
                    if 'triangle' in large_scene['objects']:
                        del large_scene['objects']['triangle']
                    if 'disk' not in large_scene['objects']:
                        large_scene['objects'] = {
                            'disk': {'pos': None,
                                     'normal': None,
                                     'material_idx': None}}
                    large_scene['objects']['disk']['radius'] = tch_var_f(
                        np.ones(self.opt.n_splats) * self.opt.splats_radius)
                    large_scene['objects']['disk']['material_idx'] = tch_var_l(
                        np.zeros(self.opt.n_splats, dtype=int).tolist())
                    large_scene['objects']['disk']['pos'] = Variable(
                        samples['splats']['pos'][idx].cuda(),
                        requires_grad=False)
                    large_scene['objects']['disk']['normal'] = Variable(
                        samples['splats']['normal'][idx].cuda(),
                        requires_grad=False)

            # Set camera position
            if not self.opt.same_view:
                large_scene['camera']['eye'] = tch_var_f(self.cam_pos[idx])
            else:
                large_scene['camera']['eye'] = tch_var_f(self.cam_pos[0])

            # Render scene
            #view_dir = normalize(large_scene['camera']['at'] - large_scene['camera']['eye'])
            res = render(large_scene,
                         norm_depth_image_only=self.opt.norm_depth_image_only,
                         double_sided=True, use_quartic=self.opt.use_quartic)

            # Get rendered output
            if self.opt.render_img_nc == 1:
                depth = res['depth']
                # Normalize depth image
                # cond = depth >= large_scene['camera']['far']
                # depth = where(cond, torch.min(depth), depth)
                # im_d = ((depth - torch.min(depth)) /
                #       (torch.max(depth) - torch.min(depth)))
                im_d = depth.unsqueeze(0)
            else:
                depth = res['depth']

                im_d = depth.unsqueeze(0)
                im = res['image'].permute(2, 0, 1)

                target_normal_ = get_data(res['normal'])
                target_normalmap_img_ = get_normalmap_image(target_normal_)
                im_n = tch_var_f(
                    target_normalmap_img_).view(im.shape[1], im.shape[2],
                                                3).permute(2, 0, 1)

            # Add depth image to the output structure
            if self.iterationa_no % self.opt.save_image_interval == 0:
                imsave(inpath + str(self.iterationa_no) + 'real_normalmap_{:05d}.png'.format(idx), target_normalmap_img_)
                imsave(inpath + str(self.iterationa_no) + 'real_depth_{:05d}.png'.format(idx), get_data(depth))
                #imsave(inpath + str(self.iterationa_no) + 'real_depthmap_{:05d}.png'.format(idx), im_d)
                # imsave(inpath + str(self.iterationa_no) + 'world_normalmap_{:05d}.png'.format(idx), target_worldnormalmap_img_)
            data.append(im)
            data_depth.append(im_d)
            data_normal.append(im_n)
            data_cond.append(large_scene['camera']['eye'])
        # Stack real samples
        real_samples = torch.stack(data)
        real_samples_depth = torch.stack(data_depth)
        real_samples_normal = torch.stack(data_normal)
        real_samples_cond = torch.stack(data_cond)
        self.batch_size = real_samples.size(0)
        if not self.opt.no_cuda:
            real_samples = real_samples.cuda()
            real_samples_depth = real_samples_depth.cuda()
            real_samples_normal = real_samples_normal.cuda()
            real_samples_cond = real_samples_cond.cuda()


        # Set input/output variables
        self.input.resize_as_(real_samples.data).copy_(real_samples.data)
        self.input_depth.resize_as_(real_samples_depth.data).copy_(real_samples_depth.data)
        self.input_normal.resize_as_(real_samples_normal.data).copy_(real_samples_normal.data)
        self.input_cond.resize_as_(real_samples_cond.data).copy_(real_samples_cond.data)
        self.label.resize_(self.batch_size).fill_(self.real_label)
        self.inputv = Variable(self.input)
        self.inputv_depth = Variable(self.input_depth)
        self.inputv_normal = Variable(self.input_normal)
        self.inputv_cond = Variable(self.input_cond)
        self.labelv = Variable(self.label)

    def generate_noise_vector(self, ):
        """Generate a noise vector."""
        self.noise.resize_(
            self.batch_size, int(self.opt.nz), 1, 1).normal_(0, 1)
        self.noisev = Variable(self.noise)  # TODO: Add volatile=True???


    def generate_normals(self, z_batch, cam_pos, camera):
        W, H = camera['viewport'][2:]
        normals = []
        for z, eye in zip(z_batch, cam_pos):
            camera['eye'] = eye
            pcl = z_to_pcl_CC(z.squeeze(), camera)
            n = self.netG2(pcl.view(H, W, 3).permute(2, 0, 1)[np.newaxis, ...])
            n = n.squeeze().permute(1, 2, 0).view(-1, 3).contiguous()
            # if self.sph_normals:
            #     n = sph2cart_unit(n.view(-1, 2))
            normals.append(n)
        return torch.stack(normals)

    def render_batch(self, batch, batch_cond=None):
        """Render a batch of splats."""
        batch_size = batch.size()[0]

        # Generate camera positions on a sphere
        if batch_cond is None:
            if self.opt.full_sphere_sampling:
                cam_pos = uniform_sample_sphere(
                    radius=self.opt.cam_dist, num_samples=self.opt.batchSize,
                    axis=self.opt.axis, angle=np.deg2rad(self.opt.angle),
                    theta_range=self.opt.theta, phi_range=self.opt.phi)
            else:
                cam_pos = uniform_sample_sphere(
                    radius=self.opt.cam_dist, num_samples=self.opt.batchSize,
                    axis=self.opt.axis, angle=self.opt.angle,
                    theta_range=np.deg2rad(self.opt.theta), phi_range=np.deg2rad(self.opt.phi))


        rendered_data = []
        rendered_data_depth = []
        rendered_data_normal = []
        rendered_data_world_normal = []
        rendered_data_cond = []
        rendered_res_world = []
        scenes = []
        inpath = self.opt.out_dir + '/'
        z_min = self.scene['camera']['focal_length']
        z_max = z_min + 3

        # TODO (fmannan): Move this in init. This only needs to be done once!
        # Set splats into rendering scene
        if 'sphere' in self.scene['objects']:
            del self.scene['objects']['sphere']
        if 'triangle' in self.scene['objects']:
            del self.scene['objects']['triangle']
        if 'disk' not in self.scene['objects']:
            self.scene['objects'] = {'disk': {'pos': None, 'normal': None,
                                              'material_idx': None}}
        lookat = self.opt.at if self.opt.at is not None else [0.0, 0.0, 0.0, 1.0]
        self.scene['camera']['at'] = tch_var_f(lookat)
        self.scene['objects']['disk']['material_idx'] = tch_var_l(
            np.zeros(self.opt.splats_img_size * self.opt.splats_img_size))
        loss = 0.0
        loss_ = 0.0
        z_loss_ = 0.0
        z_norm_loss_ = 0.0
        spatial_loss_ = 0.0
        spatial_var_loss_ = 0.0
        unit_normal_loss_ = 0.0
        normal_away_from_cam_loss_ = 0.0
        image_depth_consistency_loss_ = 0.0
        for idx in range(batch_size):
            # Get splats positions and normals
            eps = 1e-3
            if self.opt.rescaled:
                z = F.relu(-batch[idx][:, 0]) + z_min
                z = (z - z.min()) / (z.max() - z.min() + eps) * (z_max - z_min) + z_min
                pos = -z
            else:
                pos = -F.relu(-batch[idx][:, 0]) - z_min
            normals = batch[idx][:, 1:]

            self.scene['objects']['disk']['pos'] = pos

            # Normal estimation network and est_normals don't go together
            #assert self.opt.est_normals is False
            self.scene['objects']['disk']['normal'] = normals if self.opt.est_normals is False else None

            # Set camera position
            if batch_cond is None:
                if not self.opt.same_view:
                    self.scene['camera']['eye'] = tch_var_f(cam_pos[idx])
                else:
                    self.scene['camera']['eye'] = tch_var_f(cam_pos[0])
            else:
                if not self.opt.same_view:
                    self.scene['camera']['eye'] = batch_cond[idx]
                else:
                    self.scene['camera']['eye'] = batch_cond[0]

            # Render scene
            # res = render_splats_NDC(self.scene)
            res = render_splats_along_ray(self.scene, samples=self.opt.pixel_samples, normal_estimation_method='plane')

            world_tform = cam_to_world(res['pos'].view((-1, 3)), res['normal'].view((-1, 3)), self.scene['camera'])

            # Get rendered output
            res_pos = res['pos'].contiguous()
            res_pos_2D = res_pos.view(res['image'].shape)
            # With supersampling the z_loss needs to be applied after supersampling
                # TODO: Enable this (currently final loss becomes NaN!!)
                #loss += torch.mean(
                #    (10 * F.relu(z_min - torch.abs(res_pos[..., 2]))) ** 2 +
                #    (10 * F.relu(torch.abs(res_pos[..., 2]) - z_max)) ** 2)

            spatial_loss = spatial_3x3(res_pos_2D)
            res_normal = res['normal']
            depth_grad_loss = spatial_3x3(res['depth'][..., np.newaxis])
            grad_img = grad_spatial2d(torch.mean(res['image'], dim=-1)[..., np.newaxis])
            grad_depth_img = grad_spatial2d(res['depth'][..., np.newaxis])
            image_depth_consistency_loss = depth_rgb_gradient_consistency(res['image'], res['depth'])
            unit_normal_loss = unit_norm2_L2loss(res_normal, 10.0)
            normal_away_from_cam_loss = away_from_camera_penalty(res_pos, res_normal)
            z_pos = res_pos[..., 2]
            z_loss = torch.mean((2 * F.relu(z_min - torch.abs(z_pos))) ** 2 + (2 * F.relu(torch.abs(z_pos) - z_max)) ** 2)
            z_norm_loss = normal_consistency_cost(res_pos, res['normal'], norm=1)
            spatial_var = torch.mean(res_pos[..., 0].var() + res_pos[..., 1].var() + res_pos[..., 2].var())
            spatial_var_loss = (1 / (spatial_var + 1e-4))




            loss =  self.opt.zloss* z_loss + self.opt.unit_normalloss*unit_normal_loss + \
                self.opt.normal_consistency_loss_weight * z_norm_loss + \
                self.opt.spatial_var_loss_weight * spatial_var_loss + \
                self.opt.grad_img_depth_loss * image_depth_consistency_loss + self.opt.spatial_loss_weight * spatial_loss
            pos_out_ = get_data(res['pos'])

            loss_ += get_data(loss)
            z_loss_ += get_data(z_loss)
            z_norm_loss_ += get_data(z_norm_loss)
            spatial_loss_ += get_data(spatial_loss)
            spatial_var_loss_ += get_data(spatial_var_loss)
            unit_normal_loss_ += get_data(unit_normal_loss)
            normal_away_from_cam_loss_ += get_data(normal_away_from_cam_loss)
            image_depth_consistency_loss_ += get_data(image_depth_consistency_loss)

            normals_ = get_data(res_normal)


            if self.opt.render_img_nc == 1:
                depth = res['depth']
                # Normalize depth image
                # cond = depth >= self.scene['camera']['far']
                # depth = where(cond, torch.min(depth), depth)
                # im = ((depth - torch.min(depth)) /
                #       (torch.max(depth) - torch.min(depth)))
                im = depth.unsqueeze(0)
            else:
                depth = res['depth']
                # Normalize depth image
                #im_d = ((depth - torch.min(depth)) /
                #       (torch.max(depth) - torch.min(depth)))
                #im_d = where(cond, torch.min(depth), depth)
                im_d = depth.unsqueeze(0)
                im = res['image'].permute(2, 0, 1)
                H, W = im.shape[1:]
                target_normal_ = get_data(res['normal']).reshape((H, W, 3))
                target_normalmap_img_ = get_normalmap_image(target_normal_)

                target_worldnormal_ = get_data(world_tform['normal']).reshape((H, W, 3))
                target_worldnormalmap_img_ = get_normalmap_image(target_worldnormal_)
            if self.iterationa_no % self.opt.save_image_interval == 0:
                imsave(inpath+ str(self.iterationa_no)+ 'normalmap_{:05d}.png'.format(idx), target_normalmap_img_)
                imsave(inpath+ str(self.iterationa_no)+ 'depthmap_{:05d}.png'.format(idx), get_data(res['depth']))

                imsave(inpath+ str(self.iterationa_no)+ 'world_normalmap_{:05d}.png'.format(idx), target_worldnormalmap_img_)
            if self.iterationa_no % 200 == 0:
                im2 = get_data(res['image'])
                depth2 = get_data(res['depth'])
                pos = get_data(res['pos'])

                out_file2 = ("pos"+".npy")
                np.save(inpath+out_file2, pos)

                out_file2 = ("im"+".npy")
                np.save(inpath+out_file2, im2)

                out_file2 = ("depth"+".npy")
                np.save(inpath+out_file2, depth2)

                #save xyz file
                save_xyz(inpath + str(self.iterationa_no) + 'withnormal_{:05d}.xyz'.format(idx),
                         pos=get_data(res['pos']), normal=get_data(res['normal']))

                #save xyz file in world co-ordinates
                save_xyz(inpath + str(self.iterationa_no) + 'withnormal_world_{:05d}.xyz'.format(idx),
                         pos=get_data(world_tform['pos']), normal=get_data(world_tform['normal']))
            if self.opt.gz_gi_loss is not None and self.opt.gz_gi_loss > 0:
                gradZ = grad_spatial2d(res_pos_2D[:, :, 2][:, :, np.newaxis])
                gradImg = grad_spatial2d(torch.mean(im, dim=0)[:, :, np.newaxis])
                for (gZ, gI) in zip(gradZ, gradImg):
                    loss += self.opt.gz_gi_loss * torch.mean(torch.abs(torch.abs(gZ) - torch.abs(gI)))
            # Store normalized depth into the data
            rendered_data.append(im)
            rendered_data_depth.append(im_d)
            #rendered_res_world.append(dict_res_world)
            rendered_data_cond.append(self.scene['camera']['eye'])
            scenes.append(self.scene)
            #scenes.append(self.scene)

        rendered_data = torch.stack(rendered_data)
        rendered_data_depth = torch.stack(rendered_data_depth)

        if self.iterationa_no % self.opt.print_interval == 0:

            z__ = pos_out_[..., 2]
            print('%d. loss= %f nloss=%f z_loss=%f  [%f, %f], z_normal_loss: %f,'
                  ' spatial_var_loss: %f, normal_away_loss: %f'
                  ' nz_range: [%f, %f], spatial_loss: %f, imd_loss: %f' %
                  (self.iterationa_no, loss_/self.opt.batchSize, unit_normal_loss_/self.opt.batchSize, z_loss_/self.opt.batchSize,  np.min(z__),
                   np.max(z__), z_norm_loss_/self.opt.batchSize, spatial_var_loss_/self.opt.batchSize, normal_away_from_cam_loss_/self.opt.batchSize,
                   normals_[..., 2].min(), normals_[..., 2].max(), spatial_loss_/self.opt.batchSize, image_depth_consistency_loss_/self.opt.batchSize))
            self.output_loss_file.write('%d. loss= %f nloss=%f z_loss=%f  [%f, %f], z_normal_loss: %f,'
                  ' spatial_var_loss: %f, normal_away_loss: %f'
                  ' nz_range: [%f, %f], spatial_loss: %f, imd_loss: %f' %
                  (self.iterationa_no, loss_/self.opt.batchSize, unit_normal_loss_/self.opt.batchSize, z_loss_/self.opt.batchSize,  np.min(z__),
                   np.max(z__), z_norm_loss_/self.opt.batchSize, spatial_var_loss_/self.opt.batchSize, normal_away_from_cam_loss_/self.opt.batchSize,
                   normals_[..., 2].min(), normals_[..., 2].max(), spatial_loss_/self.opt.batchSize, image_depth_consistency_loss_/self.opt.batchSize))
            self.output_loss_file.flush()
        return rendered_data, rendered_data_depth, loss/self.opt.batchSize

    def train(self, ):
        """Train network."""
        # Load pretrained model if required
        if self.opt.gen_model_path is not None:
            print("Reloading networks from")
            print(' > Generator', self.opt.gen_model_path)
            self.netG.load_state_dict(
                torch.load(open(self.opt.gen_model_path, 'rb')))
            print(' > Generator2', self.opt.gen_model_path2)
            self.netG2.load_state_dict(
                torch.load(open(self.opt.gen_model_path2, 'rb')))
            print(' > Discriminator', self.opt.dis_model_path)
            self.netD.load_state_dict(
                torch.load(open(self.opt.dis_model_path, 'rb')))
            print(' > Discriminator2', self.opt.dis_model_path2)
            self.netD2.load_state_dict(
                torch.load(open(self.opt.dis_model_path2, 'rb')))
            # iteration=0

        # Start training
        file_name = os.path.join(self.opt.out_dir, 'L2.txt')
        with open(file_name, 'wt') as l2_file:
            curr_generator_idx = 0
            for iteration in range(self.opt.n_iter):
                self.iterationa_no = iteration

                # Train Discriminator critic_iters times
                for j in range(self.opt.critic_iters):
                    # Train with real
                    #################
                    self.netD.zero_grad()
                    self.get_real_samples()
                    # input_D = torch.cat([self.inputv, self.inputv_depth], 1)
                    real_output = self.netD(self.inputv, self.inputv_cond)

                    if self.opt.criterion == 'GAN':
                        errD_real = self.criterion(real_output, self.labelv)
                        errD_real.backward()
                    elif self.opt.criterion == 'WGAN':
                        errD_real = real_output.mean()
                        errD_real.backward(self.mone)
                    else:
                        raise ValueError('Unknown GAN criterium')

                    # Train with fake
                    #################
                    self.generate_noise_vector()
                    fake_z = self.netG(self.noisev, self.inputv_cond)
                    # The normal generator is dependent on z
                    fake_n = self.generate_normals(fake_z, self.inputv_cond, self.scene['camera'])
                    fake = torch.cat([fake_z, fake_n], 2)
                    fake_rendered, fd, loss = self.render_batch(fake, self.inputv_cond)
                    # Do not bp through gen
                    outD_fake = self.netD(fake_rendered.detach(), self.inputv_cond.detach())
                    if self.opt.criterion == 'GAN':
                        labelv = Variable(self.label.fill_(self.fake_label))
                        errD_fake = self.criterion(outD_fake, labelv)
                        errD_fake.backward()
                        errD = errD_real + errD_fake
                    elif self.opt.criterion == 'WGAN':
                        errD_fake = outD_fake.mean()
                        errD_fake.backward(self.one)
                        errD = errD_fake - errD_real
                    else:
                        raise ValueError('Unknown GAN criterium')

                    # Compute gradient penalty
                    if self.opt.gp != 'None':
                        gradient_penalty = calc_gradient_penalty(
                            self.netD, self.inputv.data, fake_rendered.data,
                            self.inputv_cond.data, self.opt.gp_lambda)
                        gradient_penalty.backward()
                        errD += gradient_penalty


                    gnorm_D = torch.nn.utils.clip_grad_norm(
                        self.netD.parameters(), self.opt.max_gnorm)

                    # Update weight
                    self.optimizerD.step()
                    # Clamp critic weigths if not GP and if WGAN
                    if self.opt.criterion == 'WGAN' and self.opt.gp == 'None':
                        for p in self.netD.parameters():
                            p.data.clamp_(-self.opt.clamp, self.opt.clamp)

                ############################
                # (2) Update G network
                ###########################
                # To avoid computation
                # for p in self.netD.parameters():
                #     p.requires_grad = False
                self.netG.zero_grad()

                self.generate_noise_vector()
                fake_z = self.netG(self.noisev, self.inputv_cond)
                fake_n = self.generate_normals(fake_z, self.inputv_cond, self.scene['camera'])
                fake = torch.cat([fake_z, fake_n], 2)
                fake_rendered, fd, loss = self.render_batch(
                    fake, self.inputv_cond)
                outG_fake = self.netD(fake_rendered, self.inputv_cond)


                if self.opt.criterion == 'GAN':
                    # Fake labels are real for generator cost
                    labelv = Variable(self.label.fill_(self.real_label))
                    errG = self.criterion(outG_fake, labelv)
                    errG.backward()
                elif self.opt.criterion == 'WGAN':
                    errG = outG_fake.mean() +loss
                    errG.backward(self.mone)
                else:
                    raise ValueError('Unknown GAN criterium')
                gnorm_G = torch.nn.utils.clip_grad_norm(
                    self.netG.parameters(), self.opt.max_gnorm)
                if (self.opt.alt_opt_zn_interval is not None and iteration >= self.opt.alt_opt_zn_start):
                    # update one of the generators
                    if (iteration - self.opt.alt_opt_zn_start) % self.opt.alt_opt_zn_interval == 0:
                        # switch generator vars to optimize
                        curr_generator_idx = (1 - curr_generator_idx)
                    if iteration < self.opt.lr_iter:
                        self.LR_SCHED_MAP[curr_generator_idx].step()
                        self.OPT_MAP[curr_generator_idx].step()
                else:
                    if iteration < self.opt.lr_iter:
                        self.optG_z_lr_scheduler.step()

                    self.optimizerG.step()


                mse_criterion = nn.MSELoss().cuda()
                # Log print
                if iteration % self.opt.print_interval == 0:

                    l2_loss = mse_criterion(fd, self.inputv_depth)
                    Wassertein_D = (errD_real.data[0] - errD_fake.data[0])
                    #Wassertein_D_depth = (errD_real_depth.data[0] - errD_fake_depth.data[0])
                    print('\n[%d/%d] Loss_D: %.4f Loss_G: %.4f Loss_D_real: %.4f'
                          ' Loss_D_fake: %.4f Wassertein D: %.4f '
                          ' L2_loss: %.4f z_lr: %.8f, n_lr: %.8f' % (
                          iteration, self.opt.n_iter, errD.data[0],
                          errG.data[0], errD_real.data[0], errD_fake.data[0],
                          Wassertein_D, loss.data[0],
                          self.optG_z_lr_scheduler.get_lr()[0], self.optG2_normal_lr_scheduler.get_lr()[0]))
                    l2_file.write('%s\n' % (str(l2_loss.data[0])))
                    l2_file.flush()
                    print("written to file", str(l2_loss.data[0]))

                    if self.exp is not None:
                        self.exp.metric("iteration", iteration)
                        self.exp.metric("loss D", errD.data[0])
                        self.exp.metric("loss G", errG.data[0])
                        self.exp.metric("Loss D real", errD_real.data[0])
                        self.exp.metric("Loss D fake", errD_fake.data[0])
                        self.exp.metric("Wassertein D", Wassertein_D)

                # Save output images
                if iteration % self.opt.save_image_interval == 0:
                    cs = tch_var_f(contrast_stretch_percentile(get_data(fd),  200, [fd.data.min(), fd.data.max()]))
                    torchvision.utils.save_image(fake_rendered.data, os.path.join(self.opt.out_dir,  'output_%d.png' % (iteration)), nrow=2, normalize=True, scale_each=True)

                # Save input images
                if iteration % (self.opt.save_image_interval*2) == 0:
                    cs = tch_var_f(contrast_stretch_percentile(get_data(fd),  200, [fd.data.min(), fd.data.max()]))
                    torchvision.utils.save_image(self.inputv.data, os.path.join(self.opt.out_dir,  'input_%d.png' % (iteration)), nrow=2, normalize=True, scale_each=True)

                # Do checkpointing
                if iteration % self.opt.save_interval == 0:
                    self.save_networks(iteration)

    def save_networks(self, epoch):
        """Save networks to hard disk."""
        torch.save(self.netG.state_dict(),
                   '%s/netG_epoch_%d.pth' % (self.opt.out_dir, epoch))
        torch.save(self.netG2.state_dict(),
                   '%s/netG2_epoch_%d.pth' % (self.opt.out_dir, epoch))
        torch.save(self.netD.state_dict(),
                   '%s/netD_epoch_%d.pth' % (self.opt.out_dir, epoch))
        torch.save(self.netD2.state_dict(),
                   '%s/netD2_epoch_%d.pth' % (self.opt.out_dir, epoch))

    def save_images(self, epoch, input, output):
        """Save images."""
        if self.opt.render_img_nc == 1:
            imsave(self.opt.out_dir + '/input2' + str(epoch) + '.png',
                   np.uint8(255. * input.cpu().data.numpy().squeeze()))
            imsave(self.opt.out_dir + '/fz' + str(epoch) + '.png',
                   np.uint8(255. * output.cpu().data.numpy().squeeze()))
        else:
            imsave(self.opt.out_dir + '/input2' + str(epoch) + '.png',
                   np.uint8(255. * input.cpu().data.numpy().transpose((1, 2, 0))))
            imsave(self.opt.out_dir + '/output2' + str(epoch) + '.png',
                   np.uint8(255. * output.cpu().data.numpy().transpose((1, 2, 0))))


def main():
    """Start training."""
    # Parse args
    opt = Parameters().parse()

    # Create experiment output folder
    exp_dir = os.path.join(opt.out_dir, opt.name)
    mkdirs(exp_dir)

    # Copy scripts to experiment folder
    copy_scripts_to_folder(exp_dir)

    # Save parameters to experiment folder
    file_name = os.path.join(exp_dir, 'opt.txt')
    args = vars(opt)
    with open(file_name, 'wt') as opt_file:
        opt_file.write('------------ Options -------------\n')
        for k, v in sorted(args.items()):
            opt_file.write('%s: %s\n' % (str(k), str(v)))
        opt_file.write('-------------- End ----------------\n')

    # Create the hyperdash logger if available
    exp = None
    if HYPERDASH_SUPPORTED:
        # create new Hyperdash logger
        exp = Experiment("inverse graphics")

        # log all the parameters for this experiment
        for key, val in opt.__dict__.items():
            exp.param(key, val)

    # Create dataset loader
    if opt.toy_example:
        dataset_load = None
    else:
        dataset_load = Dataset_load(opt)

    # Create GAN
    gan = GAN(opt, dataset_load, exp, exp_dir)

    # Train gan
    gan.train()

    # Finsih Hyperdash logger
    if exp is not None:
        exp.end()


if __name__ == '__main__':
    main()