from collections import OrderedDict
import torch
import torch.nn as nn
from ..layers.identity import Identity
from ..layers.activations import get_activation
from ..layers.convolutional import *#ConvELU2D, ConvELU3D, Conv2D, Conv3D
from ..layers.sampling import Upsample as InfernoUpsample
from ...utils.math_utils import max_allowed_ds_steps


__all__ = ['UNetBase', 'UNet', 'ResBlockUNet']
_all = __all__













class UNetBase(nn.Module):

    """ Base class for implementing UNets.
        The depth and dimension of the UNet is flexible.
        The deriving classes must implement
        `conv_op_factory` and can implement
        `upsample_op_factory`,
        `downsample_op_factory` and
        .

    Attributes:
        dim (int): Spatial dimension of data (must be 1, 2 or 3).
        in_channels (int): Number of input channels.
        initial_features (int): Number of desired features after initial conv block
        out_channels (int): Number of output channels. Set to None by default,
            which sets the number of output channels to get_num_channels(0) x initial (default: None).
        depth (int): How many down-sampling / up-sampling steps
            shall be performed (default: 3).
        gain (int): Multiplicative increase of channels while going down in the UNet.
            The same factor is used to decrease the number of channels while
            going up in the UNet (default: 2).
            If UNetBase.get_num_channels is overwritten, this parameter becomes meaningless.
    """

    def __init__(self, dim, in_channels, initial_features, out_channels=None, depth=3,
                 gain=2, residual=False, upsample_mode=None):

        super(UNetBase, self).__init__()

        # early sanity check
        if dim not in [1, 2, 3]:
            raise RuntimeError("UNetBase is only implemented for 1D, 2D and 3D")

        # settings related members
        self.in_channels        = int(in_channels)
        self.initial_features   = int(initial_features)
        self.dim                = int(dim)

        if out_channels is None:
            self.out_channels = self.get_num_channels(1)
        else:
            self.out_channels = int(out_channels)

        self.depth        = int(depth)
        self.gain         = int(gain)
           

        # members to remember what to store as side output
        self._side_out_num_channels = OrderedDict()
        # and number of channels per side output
        self.n_channels_per_output = None 


        # members to hold actual nn.Modules / nn.ModuleLists
        self._conv_start_op = None
        self._conv_down_ops  = None
        self._downsample_ops = None
        self._conv_bottom_op = None
        self._upsample_ops = None
        self._conv_up_ops = None
        self._conv_end_op = None

        # upsample kwargs
        self._upsample_kwargs = self._make_upsample_kwargs(upsample_mode=upsample_mode)



        # initialize all parts of the unet
        # (do not change order since we use ordered dict
        # to remember number of out channels of side outs)
        # - convs
        self._init_start()
        self._init_downstream()
        self._init_bottom()
        self._init_upstream()
        self._init_end()
        # - pool/upsample downsample
        self._init_downsample()
        self._init_upsample()

        # side out related 
        n_outputs = len(self._side_out_num_channels)
        self.out_channels_side_out = tuple(self._side_out_num_channels.values())

    def get_num_features(self, part, depth_index):
        if part in ('down','up'):
            return self.initial_features * self.gain**(depth_index + 1)
        elif part == 'bottom':
            return self.initial_features * self.gain**(self.depth + 1)
        else:
            raise RuntimeError('"{0}"  is a wrong part for "get_num_features"' .format(part))

    def _init_downstream(self):
        conv_down_ops = []
        self._store_conv_down = []

        current_in_channels = self.initial_features

        for depth_index in range(self.depth):
            out_channels = self.get_num_features(part='down', depth_index=depth_index)
            op, return_op_res = self.conv_op_factory(in_channels=current_in_channels,
                                                     out_channels=out_channels,
                                                     part='down', depth_index=depth_index)
            conv_down_ops.append(op)
            if return_op_res:
                self._side_out_num_channels[('down', depth_index)] = out_channels
    
            # increase the number of channels
            current_in_channels = out_channels

        # store as proper torch ModuleList
        self._conv_down_ops = nn.ModuleList(conv_down_ops)

    def _init_downsample(self):
        # pooling / downsample operators
        self._downsample_ops = nn.ModuleList([
            self.downsample_op_factory(i) for i in range(self.depth)
        ])

    def _init_upsample(self):
        # upsample operators
        # we flip the index that is given as argument to index consistently in up and
        # downstream sampling factories
        self._upsample_ops = nn.ModuleList([
            self.upsample_op_factory(self._inv_index(i)) for i in range(self.depth)
        ])

    def _init_bottom(self):

        in_channels = self.get_num_features(part='down', depth_index=self.depth-1)
        out_channels = self.get_num_features(part='bottom', depth_index=None)
        op, return_op_res = self.conv_op_factory(in_channels=in_channels,
            out_channels=out_channels, part='bottom', depth_index=None)
        self._conv_bottom_op = op
        if return_op_res:
            self._side_out_num_channels['bottom'] = out_channels


    def _init_upstream(self):
        conv_up_ops = []
        in_channels_from_below = self.get_num_features(part='bottom', depth_index=None)

        for i in range(self.depth):

            # at which depth are we
            depth_index = self._inv_index(i)
            
            # in/out number of channels
            in_channels_from_left = self.get_num_features(part='down', depth_index=depth_index)
            in_channels = in_channels_from_below + in_channels_from_left
            out_channels = self.get_num_features(part='up', depth_index=depth_index)


            # we flip the index that is given as argument to index consistently in up and
            # downstream conv factories
            op, return_op_res = self.conv_op_factory(in_channels=in_channels,
                                                     out_channels=out_channels,
                                                     part='up', depth_index=depth_index)
            conv_up_ops.append(op)
            if return_op_res:
                self._side_out_num_channels[('up', depth_index)] = out_channels

            # decrease the number of input_channels
            in_channels_from_below = out_channels

        # store as proper torch ModuleLis
        self._conv_up_ops = nn.ModuleList(conv_up_ops)

    def _init_start(self):
        conv, return_op_res = self.conv_op_factory(in_channels=self.in_channels,
                                                     out_channels=self.initial_features,
                                                     part='start', depth_index=None)
        if return_op_res:
            self._side_out_num_channels['start'] = self.initial_features

        self._start_block = conv 
    
    def _init_end(self):
        print('in for end', self.get_num_features(part='up', depth_index=0))
        conv, return_op_res = self.conv_op_factory(in_channels=self.get_num_features(part='up', depth_index=0),
                                                   out_channels=self.out_channels,
                                                     part='end',depth_index=None)
        # since this is the very last layer of the unet
        # we ALWAYS return the result of this op
        # and ignore return_op_res
        self._side_out_num_channels['end'] = self.out_channels

        self._end_block = conv  

    def _make_upsample_kwargs(self, upsample_mode):
        """To avoid some waring from pytorch, and some missing implementations
        for the arguments need to be handle carefully in this helper functions

        Args:
            upsample_mode (str): users choice for upsampling  interpolation style.
        """
        if upsample_mode is None:
            if self.dim == 1:
                upsample_mode = 'linear'
            elif self.dim == 2:
                upsample_mode = 'bilinear'
            elif self.dim == 3:
                # upsample_mode = 'nearest'
                upsample_mode = 'trilinear'

        upsample_kwargs = dict(scale_factor=2, mode=upsample_mode)
        if upsample_mode in ('linear','bilinear', 'trilinear'):
            upsample_kwargs['align_corners'] = False
        return upsample_kwargs

    def _forward_sanity_check(self, input):
        if isinstance(input, tuple):
            raise RuntimeError("tuples of tensors are not supported")
        shape = input.shape

        if shape[1] != self.in_channels:
            raise RuntimeError("wrong number of channels: expected %d, got %d"%
                (self.in_channels, input.size(1)))

        if input.dim() != self.dim + 2:
            raise RuntimeError("wrong number of dim: expected %d, got %d"%
                (self.dim+2, input.dim()))
        self._check_scaling(input)

    # override if model has different scaling
    def _check_scaling(self, input):
        shape = input.shape
        mx = max_allowed_ds_steps(shape=shape[2:2+self.dim], factor=2)
        if mx < self.depth:
            raise RuntimeError("cannot downsample %d times, with shape %s"%
                (self.depth, str(input.size())) )

    def forward(self, input):

        # check if input is suitable
        self._forward_sanity_check(input=input)

        # collect all desired outputs
        side_out = []

        # remember all conv-block results of the downward part
        # of the UNet
        down_res = []

        #################################
        # downwards part
        #################################
        out = input
        out = self._start_block(out)
        if 'start' in  self._side_out_num_channels:
            side_out.append(out)
        for d in range(self.depth):

            out = self._conv_down_ops[d](out)
            assert out.size()[1] == self.get_num_features(part='down', depth_index=d)

            down_res.append(out)

            if ('down',d) in  self._side_out_num_channels:
                side_out.append(out)

            out = self._downsample_ops[d](out)

        #################################
        # bottom part
        #################################
        assert out.size()[1] == self.get_num_features(part='down', depth_index=self.depth - 1)
        out = self._conv_bottom_op(out)
        if 'bottom' in  self._side_out_num_channels:
                side_out.append(out)
        print(out.size())
        assert out.size()[1] == self.get_num_features(part='bottom', depth_index=None)

        #################################
        # upward part
        #################################
        down_res = list(reversed(down_res)) # <- eases indexing
        for d in range(self.depth):

            # upsample
            out = self._upsample_ops[d](out)

            # the result of the downward part
            a = down_res[d]
            assert a.size()[1] == self.get_num_features(part='down', depth_index=self._inv_index(d))

            # concat!
            out = torch.cat([a, out], 1)

            # the convolutional block
            out = self._conv_up_ops[d](out)
            assert out.size()[1] == self.get_num_features(part='up', depth_index=self._inv_index(d))

            if ('up', self._inv_index(d)) in  self._side_out_num_channels:
                side_out.append(out)

        assert out.size()[1] == self.get_num_features(part='up', depth_index=0)
        out  = self._end_block(out)
        #always return last block ``if 'end' in  self._side_out_num_channels:``
        side_out.append(out)

        # if we only have the last layer as output
        # we return a single tensor, otherwise a tuple of
        # tensor
        if len(side_out) == 1:
            return side_out[0]
        else:
            return tuple(side_out)

    def downsample_op_factory(self, index, in_channels=None, out_channels=None):
        if self.dim == 1:
            return nn.MaxPool1d(kernel_size=2, stride=2)
        elif self.dim == 2:
            return nn.MaxPool2d(kernel_size=2, stride=2)
        elif self.dim == 3:
            return nn.MaxPool3d(kernel_size=2, stride=2)
        else:
            # should be nonreachable
            assert False

    def upsample_op_factory(self, index, in_channels=None, out_channels=None):
        return InfernoUpsample(**self._upsample_kwargs)
        #return nn.Upsample(**self._upsample_kwargs)

    def conv_op_factory(self, in_channels, out_channels, part, depth_index):
        raise NotImplementedError("conv_op_factory need to be implemented by deriving class")


    def downsample_conv_op_factory(self, in_channels, out_channels, part, depth_index):
        if part == 'down':
            return nn.Sequential(self._downsample_ops[depth_index], self._conv_down_ops[depth_index])
        elif part == 'bottom':
            return nn.Sequential(self._downsample_ops[depth_index], self._conv_bottom_op)


    def _inv_index(self, index):
        # we flip the index that is given as argument to index consistently in up and
        # downstream conv factories
        return self.depth - 1 - index





# TODO implement function to load a pretrained unet
class UNet(UNetBase):
    """
    Default 2d / 3d U-Net implementation following (without cropping):
    https://arxiv.org/abs/1505.04597
    """
    def __init__(self, in_channels, out_channels, dim,
                 depth=4, initial_features=64, gain=2,
                 final_activation=None):
        # convolutional types for inner convolutions and output convolutions
        self.dim = dim
        self.default_conv = ConvELU


        # init the base class
        super(UNet, self).__init__(in_channels=in_channels, initial_features=initial_features, dim=dim,
                                   out_channels=out_channels, depth=depth, gain=gain)


        # get the final output and activation activation
        activation = get_activation(final_activation)
 

    def conv_op_factory(self, in_channels, out_channels, part, depth_index):

        # is this the first convolutional block?
        first = (part == 'down' and depth_index == 0)

        # initial block and  first down block just have one convolution
        if  part == 'start' or (part == 'down' and depth_index == 0):
            conv = self.default_conv(self.dim, in_channels, out_channels, 3)
        # end block is just a 1x1 convolution
        elif part == 'end':
            conv = self.default_conv(self.dim, in_channels, self.out_channels, 1)
        # two convs in series
        else:
            conv = nn.Sequential(self.default_conv(self.dim, in_channels, out_channels, 3),
                                 self.default_conv(self.dim, out_channels, out_channels, 3))
        return conv, False
