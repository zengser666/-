# Activation functions

import torch
import torch.nn as nn
import torch.nn.functional as F
import math

# SiLU https://arxiv.org/pdf/1606.08415.pdf ----------------------------------------------------------------------------
class SiLU(nn.Module):  # export-friendly version of nn.SiLU()
    @staticmethod
    def forward(x):
        return x * torch.sigmoid(x)


class Hardswish(nn.Module):  # export-friendly version of nn.Hardswish()
    @staticmethod
    def forward(x):
        # return x * F.hardsigmoid(x)  # for torchscript and CoreML
        return x * F.hardtanh(x + 3, 0., 6.) / 6.  # for torchscript, CoreML and ONNX


class MemoryEfficientSwish(nn.Module):
    class F(torch.autograd.Function):
        @staticmethod
        def forward(ctx, x):
            ctx.save_for_backward(x)
            return x * torch.sigmoid(x)

        @staticmethod
        def backward(ctx, grad_output):
            x = ctx.saved_tensors[0]
            sx = torch.sigmoid(x)
            return grad_output * (sx * (1 + x * (1 - sx)))

    def forward(self, x):
        return self.F.apply(x)


# Mish https://github.com/digantamisra98/Mish --------------------------------------------------------------------------
class Mish(nn.Module):
    @staticmethod
    def forward(x):
        return x * F.softplus(x).tanh()


class MemoryEfficientMish(nn.Module):
    class F(torch.autograd.Function):
        @staticmethod
        def forward(ctx, x):
            ctx.save_for_backward(x)
            return x.mul(torch.tanh(F.softplus(x)))  # x * tanh(ln(1 + exp(x)))

        @staticmethod
        def backward(ctx, grad_output):
            x = ctx.saved_tensors[0]
            sx = torch.sigmoid(x)
            fx = F.softplus(x).tanh()
            return grad_output * (fx + x * sx * (1 - fx * fx))

    def forward(self, x):
        return self.F.apply(x)


# FReLU https://arxiv.org/abs/2007.11824 -------------------------------------------------------------------------------
class FReLU(nn.Module):
    def __init__(self, c1, k=3):  # ch_in, kernel
        super().__init__()
        self.conv = nn.Conv2d(c1, c1, k, 1, 1, groups=c1, bias=False)
        self.bn = nn.BatchNorm2d(c1)

    def forward(self, x):
        return torch.max(x, self.bn(self.conv(x)))


# ACON https://arxiv.org/pdf/2009.04759.pdf ----------------------------------------------------------------------------
class AconC(nn.Module):
    r""" ACON activation (activate or not).
    AconC: (p1*x-p2*x) * sigmoid(beta*(p1*x-p2*x)) + p2*x, beta is a learnable parameter
    according to "Activate or Not: Learning Customized Activation" <https://arxiv.org/pdf/2009.04759.pdf>.
    """

    def __init__(self, c1):
        super().__init__()
        # nn.Parameter的作用是：将一个不可训练的类型Tensor转换成可以训练的类型parameter
        # 并且会向宿主模型注册该参数 成为其一部分 即model.parameters()会包含这个parameter
        # 从而在参数优化的时候可以自动一起优化
        self.p1 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.p2 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.beta = nn.Parameter(torch.ones(1, c1, 1, 1))

    def forward(self, x):
        dpx = (self.p1 - self.p2) * x
        return dpx * torch.sigmoid(self.beta * dpx) + self.p2 * x


class MetaAconC(nn.Module):
    r""" ACON activation (activate or not).
    MetaAconC: (p1*x-p2*x) * sigmoid(beta*(p1*x-p2*x)) + p2*x, beta is generated by a small network
    according to "Activate or Not: Learning Customized Activation" <https://arxiv.org/pdf/2009.04759.pdf>.
    """

    def __init__(self, c1, k=1, s=1, r=16):  # ch_in, kernel, stride, r
        super().__init__()
        # 为了减少参数我们在两个中间的channel加了个缩放参数r，默认设置为16
        c2 = max(r, c1 // r)
        self.p1 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.p2 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.fc1 = nn.Conv2d(c1, c2, k, s, bias=True)
        self.fc2 = nn.Conv2d(c2, c1, k, s, bias=True)
        # self.bn1 = nn.BatchNorm2d(c2)
        # self.bn2 = nn.BatchNorm2d(c1)

    def forward(self, x):
        # 自适应函数的设计空间包含了layer-wise, channel-wise, pixel-wise这三种空间 分别对应的是层, 通道, 像素
        # 这里我们选择了channel-wise 首先分别对H, W维度求均值 然后通过两个卷积层 使得每一个通道所有像素共享一个权重
        # 最后由sigmoid激活函数求得beta
        y = x.mean(dim=2, keepdims=True).mean(dim=3, keepdims=True)
        # batch-size 1 bug/instabilities https://github.com/ultralytics/yolov5/issues/2891
        # beta = torch.sigmoid(self.bn2(self.fc2(self.bn1(self.fc1(y)))))  # bug/unstable
        beta = torch.sigmoid(self.fc2(self.fc1(y)))  # bug patch BN layers removed
        dpx = (self.p1 - self.p2) * x
        return dpx * torch.sigmoid(beta * dpx) + self.p2 * x

#DyReLU
class DyReLU(nn.Module):
    def __init__(self, channels, reduction=4, k=2, conv_type='2d'):
        super(DyReLU, self).__init__()
        self.channels = channels
        self.k = k
        self.conv_type = conv_type
        assert self.conv_type in ['1d', '2d']

        # 这里降维1/4,然后再重新升维
        self.fc1 = nn.Linear(channels, channels // reduction)
        self.relu = nn.ReLU(inplace=True)
        self.fc2 = nn.Linear(channels // reduction, 2*k)
        self.sigmoid = nn.Sigmoid()

        self.register_buffer('lambdas', torch.Tensor([1.]*k + [0.5]*k).float())
        self.register_buffer('init_v', torch.Tensor([1.] + [0.]*(2*k - 1)).float())

    def get_relu_coefs(self, x):
        theta = torch.mean(x, dim=-1)
        if self.conv_type == '2d':
            theta = torch.mean(theta, dim=-1)
        theta = self.fc1(theta)
        theta = self.relu(theta)
        theta = self.fc2(theta)
        # 归一化层（normalization layer）将输出限定在 (-1, 1)之间（采用2 * Sigmoid - 1函数就可以做到）
        theta = 2 * self.sigmoid(theta) - 1
        return theta

    def forward(self, x):
        raise NotImplementedError

class DyReLUA(DyReLU):
    def __init__(self, channels, reduction=4, k=2, conv_type='2d'):
        super(DyReLUA, self).__init__(channels, reduction, k, conv_type)
        self.fc2 = nn.Linear(channels // reduction, 2*k)

    def forward(self, x):
        assert x.shape[1] == self.channels
        # 对全部channel分别分配两组参数a, b(这两个组的函数个数都是2, 所以也就是2*2=4个参数)
        # 所以这里theta.shape = [b, 2*2]
        theta = self.get_relu_coefs(x)  # 这里是执行到normalize

        # 再经过一个初始值和残差的加权和得到最终输出(lambdas为一个控制标量, 也是一个超参数)
        relu_coefs = theta.view(-1, 2*self.k) * self.lambdas + self.init_v  # 这里是执行完 theta(x)

        # BxCxL -> LxCxBx1
        # b,c,h,w -> w,c,h,b,1
        # 维度置换的原因是需要一个矩阵相乘的数学处理
        x_perm = x.transpose(0, -1).unsqueeze(-1)

        # a^k_c=relu_coefs[:, :self.k]    b^k_c=relu_coefs[:, self.k:]
        # a^k_c(x) * x_c + b^k_c(x) -> ax + b
        # (w,c,h,b,1) * [b, 2] + [b, 2] -> (w,c,h,b,2)
        output = x_perm * relu_coefs[:, :self.k] + relu_coefs[:, self.k:]

        # LxCxBx2 -> BxCxL
        # y_c = max{a^k_c(x) * x_c + b^k_c(x)}
        # (w,c,h,b,2) -> (w,c,h,b) -> (b,c,h,w)  不改变特征图维度
        result = torch.max(output, dim=-1)[0].transpose(0, -1)

        return result

class DyReLUB(DyReLU):
    def __init__(self, channels, reduction=4, k=2, conv_type='2d'):
        super(DyReLUB, self).__init__(channels, reduction, k, conv_type)
        self.fc2 = nn.Linear(channels // reduction, 2*k*channels)

    def forward(self, x):
        assert x.shape[1] == self.channels
        # 对每组channel分别分配两组参数a, b(这两个组的函数个数都是2, 所以也就是2*2=4个参数)
        # 所以这里theta.shape = [b, 2*2*channels]
        theta = self.get_relu_coefs(x)

        # 再经过一个初始值和残差的加权和得到最终输出 shape: [b, channels, 4]
        relu_coefs = theta.view(-1, self.channels, 2*self.k) * self.lambdas + self.init_v

        if self.conv_type == '1d':
            # BxCxL -> LxBxCx1
            x_perm = x.permute(2, 0, 1).unsqueeze(-1)
            output = x_perm * relu_coefs[:, :, :self.k] + relu_coefs[:, :, self.k:]
            # LxBxCx2 -> BxCxL
            result = torch.max(output, dim=-1)[0].permute(1, 2, 0)

        elif self.conv_type == '2d':
            # BxCxHxW -> HxWxBxCx1
            x_perm = x.permute(2, 3, 0, 1).unsqueeze(-1)

            # 每组channel都有两组参数, 来对channels进行非线性操作, 维度置换的原因是需要一个矩阵相乘的数学处理
            # [h, w, b, c, 1] * [b, channels, 2] + [b, channels, 2]
            output = x_perm * relu_coefs[:, :, :self.k] + relu_coefs[:, :, self.k:]

            # 最后的maxout处理: HxWxBxCx2 -> BxCxHxW
            result = torch.max(output, dim=-1)[0].permute(2, 3, 0, 1)

        return result
