"""
This is a PyTorch implementation of MobileNetV2 architecture as described in the paper:
Inverted Residuals and Linear Bottlenecks: Mobile Networks for Classification, Detection and Segmentation.
https://arxiv.org/pdf/1801.04381

Code taken from https://github.com/tonylins/pytorch-mobilenet-v2
License: Apache Version 2.0, January 2004 http://www.apache.org/licenses/

Pre-trained ImageNet model: 'deci-model-repository/mobilenet_v2/ckpt_best.pth'
"""
from typing import Optional, Callable
import numpy as np
import torch
import torch.nn as nn
import math

from super_gradients.common.registry.registry import register_model
from super_gradients.common.object_names import Models
from super_gradients.training.models import BaseClassifier
from super_gradients.training.utils.utils import get_param


class MobileNetBase(BaseClassifier):
    def __init__(self):
        super(MobileNetBase, self).__init__()

    def replace_head(self, new_num_classes=None, new_head=None):
        if new_num_classes is None and new_head is None:
            raise ValueError("At least one of new_num_classes, new_head must be given to replace output layer.")
        if new_head is not None:
            self.classifier = new_head
        else:
            self.classifier[-1] = nn.Linear(self.classifier[-1].in_features, new_num_classes)


def conv_bn(inp, oup, stride):
    return nn.Sequential(nn.Conv2d(inp, oup, 3, stride, 1, bias=False), nn.BatchNorm2d(oup), nn.ReLU6(inplace=True))


def conv_1x1_bn(inp, oup):
    return nn.Sequential(nn.Conv2d(inp, oup, 1, 1, 0, bias=False), nn.BatchNorm2d(oup), nn.ReLU6(inplace=True))


def make_divisible(x, divisible_by=8):
    import numpy as np

    return int(np.ceil(x * 1.0 / divisible_by) * divisible_by)


class InvertedResidual(nn.Module):
    def __init__(self, inp, oup, stride, expand_ratio, grouped_conv_size=1):
        """
        :param inp: number of input channels
        :param oup: number of output channels
        :param stride: conv stride
        :param expand_ratio: expansion ratio of the hidden layer after pointwise conv
        :grouped_conv_size: number of channels per grouped convolution, for depth-wise-separable convolution, use grouped_conv_size=1
        """
        super(InvertedResidual, self).__init__()
        self.stride = stride
        assert stride in [1, 2]

        hidden_dim = int(inp * expand_ratio)
        groups = int(hidden_dim / grouped_conv_size)
        self.use_res_connect = self.stride == 1 and inp == oup

        if expand_ratio == 1:
            self.conv = nn.Sequential(
                # dw
                nn.Conv2d(hidden_dim, hidden_dim, 3, stride, 1, groups=groups, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.ReLU6(inplace=True),
                # pw-linear
                nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),
                nn.BatchNorm2d(oup),
            )
        else:
            self.conv = nn.Sequential(
                # pw
                nn.Conv2d(inp, hidden_dim, 1, 1, 0, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.ReLU6(inplace=True),
                # dw
                nn.Conv2d(hidden_dim, hidden_dim, 3, stride, 1, groups=groups, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.ReLU6(inplace=True),
                # pw-linear
                nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),
                nn.BatchNorm2d(oup),
            )

    def forward(self, x):
        if self.use_res_connect:
            return x + self.conv(x)
        else:
            return self.conv(x)


class MobileNetV2(MobileNetBase):
    def __init__(
        self,
        num_classes,
        dropout: float,
        width_mult=1.0,
        structure=None,
        backbone_mode: bool = False,
        grouped_conv_size=1,
        in_channels=3,
    ) -> object:
        super(MobileNetV2, self).__init__()
        self.in_channels = in_channels
        block = InvertedResidual
        last_channel = 1280
        # IF STRUCTURE IS NONE - USE THE DEFAULT STRUCTURE NOTED
        #                                                  t, c,  n, s    stage-0 is the first conv_bn layer
        self.interverted_residual_setting = structure or [
            [1, 16, 1, 1],  # stage-1
            [6, 24, 2, 2],  # stage-2
            [6, 32, 3, 2],  # stage-3
            [6, 64, 4, 2],  # stage-4
            [6, 96, 3, 1],  # stage-5
            [6, 160, 3, 2],  # stage-6
            [6, 320, 1, 1],
        ]  # stage-7
        #                                                                   stage-8  is the last_layer
        self.last_channel = make_divisible(last_channel * width_mult) if width_mult > 1.0 else last_channel

        curr_channels = 32
        self.features = [conv_bn(in_channels, curr_channels, 2)]
        # building inverted residual blocks
        for t, c, n, s in self.interverted_residual_setting:
            output_channel = make_divisible(c * width_mult) if t > 1 else c
            for i in range(n):
                if i == 0:
                    self.features.append(block(curr_channels, output_channel, s, expand_ratio=t, grouped_conv_size=grouped_conv_size))
                else:
                    self.features.append(block(curr_channels, output_channel, 1, expand_ratio=t, grouped_conv_size=grouped_conv_size))
                curr_channels = output_channel
        # building last several layers
        self.features.append(conv_1x1_bn(curr_channels, self.last_channel))
        # make it nn.Sequential
        self.features = nn.Sequential(*self.features)
        self.backbone_mode = backbone_mode

        if self.backbone_mode:
            self.classifier = nn.Identity()
            # TODO: remove during migration of YOLOs to the new base
            self.backbone_connection_channels = self._extract_connection_layers_input_channel_size()
        else:
            # building classifier
            self.classifier = nn.Sequential(nn.Dropout(dropout), nn.Linear(self.last_channel, num_classes))
        self._initialize_weights()

    def forward(self, x):
        x = self.features(x)
        if self.backbone_mode:
            return x
        else:
            x = x.mean(3).mean(2)
            return self.classifier(x)

    def _extract_connection_layers_input_channel_size(self):
        """
        Extracts the number of channels out when using mobilenetV2 as yolo backbone
        """
        curr_layer_input = torch.rand(1, self.in_channels, 320, 320)  # input dims are used to extract number of channels
        layers_num_to_extract = [np.array(self.interverted_residual_setting)[:stage, 2].sum() for stage in [3, 5]]
        connection_layers_input_channel_size = []
        for layer_idx, feature in enumerate(self.features):
            curr_layer_input = feature(curr_layer_input)
            if layer_idx in layers_num_to_extract:
                connection_layers_input_channel_size.append(curr_layer_input.shape[1])
        connection_layers_input_channel_size.append(self.last_channel)
        connection_layers_input_channel_size.reverse()
        return connection_layers_input_channel_size

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2.0 / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                n = m.weight.size(1)
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()

    def replace_input_channels(self, in_channels: int, compute_new_weights_fn: Optional[Callable[[nn.Module, int], nn.Module]] = None):
        from super_gradients.modules.weight_replacement_utils import replace_conv2d_input_channels

        self.features[0][0] = replace_conv2d_input_channels(conv=self.features[0][0], in_channels=in_channels, fn=compute_new_weights_fn)
        self.in_channels = self.get_input_channels()

    def get_input_channels(self) -> int:
        return self.features[0][0].in_channels


@register_model(Models.MOBILENET_V2)
class MobileNetV2Base(MobileNetV2):
    def __init__(self, arch_params):
        """
        :param arch_params: HpmStruct
            must contain: 'num_classes': int
        """
        super().__init__(
            num_classes=arch_params.num_classes,
            width_mult=1.0,
            structure=None,
            dropout=get_param(arch_params, "dropout", 0.0),
            in_channels=get_param(arch_params, "in_channels", 3),
        )


@register_model(Models.MOBILE_NET_V2_135)
class MobileNetV2_135(MobileNetV2):
    def __init__(self, arch_params):
        """
        This Model achieves–≠ 75.73% on Imagenet - similar to Resnet50
        :param arch_params: HpmStruct
            must contain: 'num_classes': int
        """
        super().__init__(
            num_classes=arch_params.num_classes,
            width_mult=1.35,
            structure=None,
            dropout=get_param(arch_params, "dropout", 0.0),
            in_channels=get_param(arch_params, "in_channels", 3),
        )


@register_model(Models.CUSTOM_MOBILENET_V2)
class CustomMobileNetV2(MobileNetV2):
    def __init__(self, arch_params):
        """
        :param arch_params:–≠ HpmStruct
            must contain:
                'num_classes': int
                'width_mult': float
                'structure' : list. specify the mobilenetv2 architecture
        """
        super().__init__(
            num_classes=arch_params.num_classes,
            width_mult=arch_params.width_mult,
            structure=arch_params.structure,
            dropout=get_param(arch_params, "dropout", 0.0),
            in_channels=get_param(arch_params, "in_channels", 3),
        )
