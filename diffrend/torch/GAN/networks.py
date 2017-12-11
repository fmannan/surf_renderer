"""Networks."""
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.nn.functional as F


def create_networks(opt):
    """Create the networks."""
    # Parameters
    ngpu = int(opt.ngpu)
    nz = int(opt.nz)
    ngf = int(opt.ngf)
    ndf = int(opt.ndf)
    nc = opt.n

    # Create generator network
    if opt.gen_type=='mlp':
        netG = _netG_mlp(ngpu, nz, ngf, nc)
    elif opt.gen_type=='resnet':
        netG = _netG_resnet(nz, nc)
    else:
        netG = _netG(ngpu, nz, ngf, nc)
    netG.apply(weights_init)
    if opt.netG != '':
        netG.load_state_dict(torch.load(opt.netG))
    print(netG)

    # Create the discriminator network

    if opt.criterion == 'WGAN':
        netD = _netD(ngpu, 1, ndf,1)
    else:
        netD = _netD(ngpu, 1, ndf)
    netD.apply(weights_init)
    if opt.netD != '':
        netD.load_state_dict(torch.load(opt.netD))
    print(netD)

    return netG, netD


# Custom weights initialization called on netG and netD
def weights_init(m):
    """Weight initializer."""
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


class _netG(nn.Module):
    def __init__(self, ngpu, nz, ngf, nc):
        super(_netG, self).__init__()
        self.ngpu = ngpu
        self.nc=nc
        self.main = nn.Sequential(

            # input is Z, going into a convolution
            nn.ConvTranspose2d(nz, ngf * 8, 4, 1, 0, bias=False),
            nn.BatchNorm2d(ngf * 8),
            nn.LeakyReLU(0.2, True),
            # state size. (ngf*8) x 4 x 4
            nn.ConvTranspose2d(ngf * 8, ngf * 4, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ngf * 4),
            nn.LeakyReLU(0.2, True),
            # state size. (ngf*4) x 8 x 8
            nn.ConvTranspose2d(ngf * 4, ngf * 4, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ngf * 4),
            nn.LeakyReLU(0.2, True),
            # state size. (ngf*2) x 16 x 16
            nn.ConvTranspose2d(ngf * 4,     ngf, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ngf),
            nn.LeakyReLU(0.2, True),
            # state size. (ngf) x 32 x 32
            nn.ConvTranspose2d(ngf,      3, 4, 2, 1, bias=False),

            # state size. (nc) x 64 x 64
        )
        self.main2 = nn.Sequential(
            # input is Z, going into a convolution

            nn.Linear(64 * 64 * 3, nc*6),
            nn.BatchNorm1d(nc*6),
            nn.LeakyReLU(0.2, True),
            nn.Linear(nc*6, nc*6)
            # state size. (nc) x 64 x 64
        )

    def forward(self, input):
        if isinstance(input.data, torch.cuda.FloatTensor) and self.ngpu > 1:
            output = nn.parallel.data_parallel(self.main, input,
                                               range(self.ngpu))
        else:
            output = self.main(input)
            out = self.main2(output.view(output.size(0),-1))
            out=out.view(out.size(0),self.nc,6)
        return out

class _netG_mlp(nn.Module):
    def __init__(self, ngpu, nz, ngf, nc):
        super(_netG_mlp, self).__init__()
        self.ngpu = ngpu
        self.nc = nc
        self.main = nn.Sequential(
            # input is Z, going into a convolution
            nn.Linear(nz, ngf*4),
            nn.BatchNorm1d(ngf*4),
            nn.LeakyReLU(0.2, True),

            nn.Linear(ngf*4, ngf*8),
            nn.BatchNorm1d(ngf*8),
            nn.LeakyReLU(0.2, True),

            nn.Linear(ngf*8, ngf*16),
            nn.BatchNorm1d(ngf*16),
            nn.LeakyReLU(0.2, True),

            nn.Linear(ngf*16, ngf*36),
            nn.BatchNorm1d(ngf*36),
            nn.LeakyReLU(0.2, True),

            nn.Linear(ngf*36,nc*6),
            # nn.BatchNorm1d(ndf*4),
            # nn.LeakyReLU(0.2, True),
            #
            # nn.Linear(ndf*4, 1)

            # state size. (nc) x 64 x 64
        )

    def forward(self, input):
        if isinstance(input.data, torch.cuda.FloatTensor) and self.ngpu > 1:
            output = nn.parallel.data_parallel(self.main, input,
                                               range(self.ngpu))
        else:
            output = self.main(input)
            output=output.view(output.size(0),self.nc,6)
        return output

DIM = 512
class ResBlock(nn.Module):

    def __init__(self):
        super(ResBlock, self).__init__()

        self.res_block = nn.Sequential(
            nn.ReLU(True),
            nn.Conv1d(DIM, DIM, 3, padding=1),#nn.Linear(DIM, DIM),

            nn.ReLU(True),
            nn.Conv1d(DIM, DIM, 3, padding=1),#nn.Linear(DIM, DIM),
        )

    def forward(self, input):
        output = self.res_block(input)
        return input + (0.3*output)

class _netG_resnet(nn.Module):

    def __init__(self,nz,nc):
        super(_netG_resnet, self).__init__()

        self.fc1 = nn.Linear(nz, DIM*6)
        self.block = nn.Sequential(
            ResBlock(),
            ResBlock(),
            ResBlock(),
            ResBlock(),
            ResBlock(),
        )
        self.conv1 = nn.Conv1d(DIM, nc, 3, padding=1)
        self.bn=nn.BatchNorm1d( nc)
        self.conv2 = nn.Conv1d(nc, nc, 1)

        self.softmax = nn.Softmax()

    def forward(self, noise):
        output = self.fc1(noise)
        output = output.view(-1, DIM, 6)
        output = self.block(output)
        output = self.conv1(output)

        output = self.bn(output)
        output = self.conv2(output)


        return output


class _netD(nn.Module):
    def __init__(self, ngpu, nc, ndf,no_sigmoid=0):
        super(_netD, self).__init__()
        self.ngpu = ngpu
        self.no_sigmoid=no_sigmoid

            # input is (nc) x 64 x 64
        self.c1=    nn.Conv2d(nc, ndf, 4, 2, 1, bias=False)

            # state size. (ndf) x 32 x 32
        self.c2=    nn.Conv2d(ndf, ndf * 2, 4, 2, 1, bias=False)

            # state size. (ndf*2) x 16 x 16
        self.c3=    nn.Conv2d(ndf * 2, ndf * 4, 4, 2, 1, bias=False)

            # state size. (ndf*4) x 8 x 8
        self.c4=    nn.Conv2d(ndf * 4, ndf * 4, 4, 2, 1, bias=False)

        self.c5=    nn.Conv2d(ndf * 4, ndf * 8, 4, 2, 1, bias=False)

            # state size. (ndf*8) x 4 x 4
        self.c6=    nn.Conv2d(ndf * 8, 1, 4, 1, 0, bias=False)



    def forward(self,x):

        x = F.leaky_relu(
        F.dropout(self.c1(x), p=0.3)
        )
        x = F.leaky_relu(
        F.dropout(self.c2(x), p= 0.3)
        )
        x = F.leaky_relu(
        F.dropout(self.c3(x), p=0.3)
        )
        # x = F.leaky_relu(
        # F.dropout(self.c4(x), p=0.3)
        # )
        x = F.leaky_relu(
        F.dropout(self.c5(x), p=0.3)
        )

        x=self.c6(x)
        x=x.view(-1, 1).squeeze(1)


        if self.no_sigmoid==1:
            return x
        else:
            return F.sigmoid(x)
