"""Genrator."""
from __future__ import absolute_import

import os
import sys
sys.path.append('../..')
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.optim as optim
import torch.utils.data
import torchvision.utils as vutils
from torch.autograd import Variable
from parameters import Parameters
from datasets import Dataset_load
from networks import create_networks
from utils import where
from diffrend.torch.params import SCENE_BASIC
from diffrend.torch.utils import tch_var_f, tch_var_l, CUDA
from diffrend.torch.renderer import render
from diffrend.utils.sample_generator import uniform_sample_mesh, uniform_sample_sphere
from diffrend.model import load_model
# from data import DIR_DATA
import argparse
import copy
import os
import numpy as np
import matplotlib.pyplot as plt
from scipy.misc import imsave


def same_view(filename,  num_samples, radius, width, height, fovy,
              focal_length, cam_pos, batch_size, verbose=False):
    """Generate random samples of an object from the same camera position.

    Randomly generate N samples on a surface and render them. The samples
    include position and normal, the radius is set to a constant.
    """
    # Load model
    obj = load_model(filename, verbose=verbose)

    # Set the splats radius
    r = np.ones(num_samples) * radius

    # Create a splats rendering scene
    large_scene = copy.deepcopy(SCENE_BASIC)

    # Define the camera parameters
    large_scene['camera']['viewport'] = [0, 0, width, height]
    large_scene['camera']['fovy'] = np.deg2rad(fovy)
    large_scene['camera']['focal_length'] = focal_length
    large_scene['objects']['disk']['radius'] = tch_var_f(r)
    large_scene['objects']['disk']['material_idx'] = tch_var_l(
        np.zeros(num_samples, dtype=int).tolist())
    large_scene['materials']['albedo'] = tch_var_f([[0.6, 0.6, 0.6]])
    large_scene['tonemap']['gamma'] = tch_var_f([1.0])  # Linear output

    # Generate camera positions on a sphere
    data = []
    for idx in range(batch_size):
        # Sample points from the 3D mesh
        v, vn = uniform_sample_mesh(obj, num_samples=num_samples)

        # Normalize the vertices
        v = (v - np.mean(v, axis=0)) / (v.max() - v.min())

        # Save the splats into the rendering scene
        large_scene['objects']['disk']['pos'] = tch_var_f(v)
        large_scene['objects']['disk']['normal'] = tch_var_f(vn)

        # TODO: This should be outside?
        large_scene['camera']['eye'] = tch_var_f(cam_pos)
        # suffix = '_{}'.format(idx)

        # Render scene
        res = render(large_scene)

        # Get render image from render.
        # im = res['image']

        # Get depth image from render.
        depth = res['depth']

        # import ipdb; ipdb.set_trace()

        # Normalize depth image
        cond = depth >= large_scene['camera']['far']
        depth = where(cond, torch.min(depth), depth)
        # depth[depth >= large_scene['camera']['far']] = torch.min(depth)
        im_depth = ((depth - torch.min(depth)) /
                    (torch.max(depth) - torch.min(depth)))

        # Add depth image to the output structure
        data.append(im_depth.unsqueeze(0))

    return torch.stack(data)


# TODO: This function is the same as the previous one except for one line. Can
# we add a parameter and combine both?
def different_views(filename, num_samples, radius, cam_dist,  width, height,
                    fovy, focal_length, batch_size, verbose=False):
    """Generate rendom samples of an object from different camera positions.

    Randomly generate N samples on a surface and render them. The samples
    include position and normal, the radius is set to a constant.
    """
    obj = load_model(filename, verbose=verbose)
    r = np.ones(num_samples) * radius
    large_scene = copy.deepcopy(SCENE_BASIC)

    large_scene['camera']['viewport'] = [0, 0, width, height]
    large_scene['camera']['fovy'] = np.deg2rad(fovy)
    large_scene['camera']['focal_length'] = focal_length
    large_scene['objects']['disk']['radius'] = tch_var_f(r)
    large_scene['objects']['disk']['material_idx'] = tch_var_l(
        np.zeros(num_samples, dtype=int).tolist())
    large_scene['materials']['albedo'] = tch_var_f([[0.6, 0.6, 0.6]])
    large_scene['tonemap']['gamma'] = tch_var_f([1.0])  # Linear output

    # generate camera positions on a sphere
    cam_pos = uniform_sample_sphere(radius=cam_dist, num_samples=batch_size)
    data = []
    for idx in range(batch_size):
        v, vn = uniform_sample_mesh(obj, num_samples=num_samples)

        # normalize the vertices
        v = (v - np.mean(v, axis=0)) / (v.max() - v.min())

        large_scene['objects']['disk']['pos'] = tch_var_f(v)
        large_scene['objects']['disk']['normal'] = tch_var_f(vn)

        large_scene['camera']['eye'] = tch_var_f(cam_pos[idx])
        # suffix = '_{}'.format(idx)

        # Render scene
        res = render(large_scene)

        # Get rendered image
        # im = res['image']

        # Get depth image
        depth = res['depth']

        # Normalize depth image.
        # TODO: Used several times. Better move to a function
        cond = depth >= large_scene['camera']['far']
        depth = where(cond, torch.min(depth), depth)
        # depth[depth >= large_scene['camera']['far']] = torch.min(depth)
        im_depth = ((depth - torch.min(depth)) /
                    (torch.max(depth) - torch.min(depth)))

        data.append(im_depth.unsqueeze(0))
    return torch.stack(data)


############################
# MAIN
###########################
# TODO: Better move to a train function and create an entry point

# Parse args
opt = Parameters().parse()

# Load dataset
# dataloader = Dataset_load(opt).get_dataloader()

# Create the networks
netG, netD = create_networks(opt)

# Create the criterion
criterion = nn.BCELoss()

# Create the tensors
input = torch.FloatTensor(opt.batchSize, 3, opt.imageSize, opt.imageSize)
noise = torch.FloatTensor(opt.batchSize, int(opt.nz), 1, 1)
fixed_noise = torch.FloatTensor(opt.batchSize, int(opt.nz), 1, 1).normal_(0, 1)
label = torch.FloatTensor(opt.batchSize)
real_label = 1
fake_label = 0

# Move everything to the GPU
if not opt.no_cuda:
    netD.cuda()
    netG.cuda()
    criterion.cuda()
    input, label = input.cuda(), label.cuda()
    noise, fixed_noise = noise.cuda(), fixed_noise.cuda()

fixed_noise = Variable(fixed_noise)

# Setup optimizer
optimizerD = optim.Adam(netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
optimizerG = optim.Adam(netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))

# Create splats rendering scene
if opt.same_view:
    cam_pos = uniform_sample_sphere(radius=opt.cam_dist, num_samples=2)
r = np.ones(opt.n) * opt.r
large_scene = copy.deepcopy(SCENE_BASIC)
large_scene['camera']['viewport'] = [0, 0, opt.width, opt.height]
large_scene['camera']['fovy'] = np.deg2rad(opt.fovy)
large_scene['camera']['focal_length'] = opt.f
large_scene['objects']['disk']['radius'] = tch_var_f(r)
large_scene['objects']['disk']['material_idx'] = tch_var_l(
    np.zeros(opt.n, dtype=int).tolist())
large_scene['materials']['albedo'] = tch_var_f([[0.6, 0.6, 0.6]])
large_scene['tonemap']['gamma'] = tch_var_f([1.0])  # Linear output
# large_scene['camera']['eye'] = tch_var_f(cam_pos[0])

# Start training
for epoch in range(opt.niter):

    ############################
    # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
    ###########################
    # train with real
    netD.zero_grad()
    # TODO: We are learning always from a single model!
    if opt.same_view:
        real_cpu = same_view(opt.model, opt.n, opt.r,  opt.width,
                             opt.height, opt.fovy, opt.f, np.copy(cam_pos[0]),
                             opt.batchSize)
    else:
        real_cpu = different_views(opt.model, opt.n, opt.r, opt.cam_dist,
                                   opt.width, opt.height, opt.fovy, opt.f,
                                   opt.batchSize)
    batch_size = real_cpu.size(0)
    if not opt.no_cuda:
        real_cpu = real_cpu.cuda()
    input.resize_as_(real_cpu.data).copy_(real_cpu.data)
    label.resize_(batch_size).fill_(real_label)
    inputv = Variable(input)
    labelv = Variable(label)

    output = netD(inputv)
    errD_real = criterion(output, labelv)
    errD_real.backward()
    D_x = output.data.mean()

    # train with fake
    noise.resize_(batch_size, int(opt.nz), 1, 1).normal_(0, 1)
    noisev = Variable(noise)
    fake = netG(noisev)

    #######################
    # Processig generator output to get image
    ########################

    # Generate camera positions on a sphere
    data = []
    if not opt.same_view:
        cam_pos = uniform_sample_sphere(radius=opt.cam_dist,
                                        num_samples=batch_size)
    for idx in range(batch_size):
        # import ipdb; ipdb.set_trace()

        # Normalize the vertices
        temp = ((fake[idx][:, :3] - torch.mean(fake[idx][:, :3], 0)) /
                (torch.max(fake[idx][:, :3]) - torch.min(fake[idx][:, :3])))

        large_scene['objects']['disk']['pos'] = temp
        large_scene['objects']['disk']['normal'] = fake[idx][:, 3:]
        if not opt.same_view:
            large_scene['camera']['eye'] = tch_var_f(cam_pos[idx])
        else:
            large_scene['camera']['eye'] = tch_var_f(cam_pos[0])
        suffix = '_{}'.format(idx)

        # Render scene
        res = render(large_scene)

        # Get image and depth result images
        im = res['image']
        depth = res['depth']

        # Normalize depth image
        cond = depth >= large_scene['camera']['far']
        depth = where(cond, torch.min(depth), depth)
        im_depth = ((depth - torch.min(depth)) /
                    (torch.max(depth) - torch.min(depth)))
        data.append(im_depth.unsqueeze(0))

    data = torch.stack(data)
    labelv = Variable(label.fill_(fake_label))
    output = netD(data.detach())  # Do not backpropagate through generator
    errD_fake = criterion(output, labelv)
    errD_fake.backward()
    D_G_z1 = output.data.mean()
    errD = errD_real + errD_fake
    optimizerD.step()

    ############################
    # (2) Update G network: maximize log(D(G(z)))
    ###########################
    netG.zero_grad()
    # Fake labels are real for generator cost
    labelv = Variable(label.fill_(real_label))
    output = netD(data)
    errG = criterion(output, labelv)
    errG.backward()
    D_G_z2 = output.data.mean()
    optimizerG.step()
    suffix = '_{}'.format(epoch)
    if epoch % 10 == 0:
        print('\n[%d/%d] Loss_D: %.4f Loss_G: %.4f D(x): %.4f'
              ' D(G(z)): %.4f / %.4f' % (epoch, opt.niter,

                                         errD.data[0], errG.data[0], D_x,
                                         D_G_z1, D_G_z2))
    if epoch % 25 == 0:
        imsave(opt.out_dir + '/img' + suffix + '.png',
               np.uint8(255. * real_cpu[0].cpu().data.numpy().squeeze()))
        imsave(opt.out_dir + '/img_depth' + suffix + '.png',
               np.uint8(255. * data[0].cpu().data.numpy().squeeze()))
        imsave(opt.out_dir + '/img1' + suffix + '.png',
               np.uint8(255. * real_cpu[1].cpu().data.numpy().squeeze()))
        imsave(opt.out_dir + '/img_depth1' + suffix + '.png',
               np.uint8(255. * data[1].cpu().data.numpy().squeeze()))

    # Do checkpointing
    if epoch % 100 == 0:
        torch.save(netG.state_dict(),
                   '%s/netG_epoch_%d.pth' % (opt.out_dir, epoch))
        torch.save(netD.state_dict(),
                   '%s/netD_epoch_%d.pth' % (opt.out_dir, epoch))
        print ("iteration ", epoch, "finished")
