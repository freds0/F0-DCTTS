from config import ConfigArgs as args
import torch
import torch.nn as nn
from torch.nn.utils import weight_norm as norm
import numpy as np
import layers as ll
import modules as mm

class TextEncoder(nn.Module):
    """
    Text Encoder
        T: (N, Cx, Tx) Text embedding (variable length)
    Returns:
        K: (N, Cx, Tx) Text Encoding for Key
        V: (N, Cx, Tx) Text Encoding for Value
    """
    def __init__(self, d_in, d_out, d_hidden):
        super(TextEncoder, self).__init__()
        self.hc_blocks = nn.ModuleList([norm(ll.Conv1d(d_in, d_hidden, 1, padding='same', activation_fn=torch.relu))])  # filter up to split into K, V
        self.hc_blocks.extend([norm(ll.Conv1d(d_hidden, d_hidden, 1, padding='same', activation_fn=None))])
        self.hc_blocks.extend([norm(ll.HighwayConv1d(d_hidden, d_hidden, 3, dilation=3**i, padding='same'))
                               for _ in range(2) for i in range(4)])
        self.hc_blocks.extend([norm(ll.HighwayConv1d(d_hidden, d_hidden, 3, dilation=1, padding='same'))
                               for i in range(2)])
        self.hc_blocks.extend([norm(ll.HighwayConv1d(d_hidden, d_hidden, 1, dilation=1, padding='same'))
                               for i in range(2)])

    def forward(self, L):
        y = L
        for i in range(len(self.hc_blocks)):
            y = self.hc_blocks[i](y)
        K, V = y.chunk(2, dim=1)  # half size for axis Cx
        return K, V

class AudioEncoder(nn.Module):
    """
    Text Encoder
        prev_audio: (N, n_mels, Ty/r) Mel-spectrogram (variable length)
    Returns:
        Q: (N, Cx, Ty/r) Audio Encoding for Query
    """

    def __init__(self, d_in, d_out, d_hidden):
        super(AudioEncoder, self).__init__()
        self.hc_blocks = nn.ModuleList([norm(ll.CausalConv1d(d_in, d_hidden, 1, activation_fn=torch.relu))])
        self.hc_blocks.extend([norm(ll.CausalConv1d(d_hidden, d_hidden, 1, activation_fn=torch.relu))
                               for _ in range(2)])
        self.hc_blocks.extend([norm(ll.CausalHighwayConv1d(d_hidden, d_hidden, 3, dilation=3**i)) # i is in [[0,1,2,3],[0,1,2,3]]
                               for _ in range(2) for i in range(4)])
        self.hc_blocks.extend([norm(ll.CausalHighwayConv1d(d_hidden, d_out, 3, dilation=3))
                               for i in range(2)])
        # self.hc_blocks.extend([ll.CausalConv1d(args.Cy, args.Cx, 1, dilation=1, activation_fn=torch.relu)]) # down #filters to dotproduct K, V

    def forward(self, S):
        Q = S
        for i in range(len(self.hc_blocks)):
            Q = self.hc_blocks[i](Q)
        return Q

class DotProductAttention(nn.Module):
    """
    Dot Product Attention
    Args:
        K: (N, Cx, Tx)
        V: (N, Cx, Tx)
        Q: (N, Cx, Ty)
    Returns:
        R: (N, Cx, Ty)
        A: (N, Tx, Ty) alignments
    """

    def __init__(self, d_hidden):
        super(DotProductAttention, self).__init__()
        self.d_k = d_hidden
        self.linear_q = ll.CustomConv1d(d_hidden, d_hidden, 1)
        self.linear_k = ll.CustomConv1d(d_hidden, d_hidden, 1)
        self.linear_v = ll.CustomConv1d(d_hidden, d_hidden, 1)

    def forward(self, Q, K, V):
        Q = torch.tanh(self.linear_q(Q))
        K = torch.tanh(self.linear_k(K))
        V = torch.tanh(self.linear_v(V))
        A = torch.softmax((torch.bmm(K.transpose(1, 2), Q)/np.sqrt(self.d_k)), dim=1) # K.T.dot(Q) -> (N, Tx, Ty)
        R = torch.bmm(V, A) # (N, Cx, Ty)
        return R, A

class AudioDecoder(nn.Module):
    """
    Dot Product Attention
    Args:
        R_: (N, Cx*2, Ty)
    Returns:
        O: (N, n_mels, Ty)
    """
    def __init__(self, d_in, d_out, d_hidden):
        super(AudioDecoder, self).__init__()
        self.hc_blocks = nn.ModuleList([norm(ll.CausalConv1d(d_in, d_hidden, 1, activation_fn=torch.relu))])
        self.hc_blocks.extend([norm(ll.CausalHighwayConv1d(d_hidden, d_hidden, 3, dilation=3**i))
                               for i in range(4)])
        self.hc_blocks.extend([norm(ll.CausalHighwayConv1d(d_hidden, d_hidden, 3, dilation=1))
                               for _ in range(2)])
        self.hc_blocks.extend([norm(ll.CausalConv1d(d_hidden, d_hidden, 1, dilation=1, activation_fn=torch.relu))
                               for _ in range(3)])
        self.hc_blocks.extend([norm(ll.CausalConv1d(d_hidden, d_out, 1, dilation=1))]) # down #filters to dotproduct K, V

    def forward(self, R_):
        Y = R_
        for i in range(len(self.hc_blocks)):
            Y = self.hc_blocks[i](Y)
        return torch.sigmoid(Y)


class PostNet(nn.Module):
    """
    Dot Product Attention
    Args:
        R_: (N, Cx*2, Ty)
    Returns:
        O: (N, n_mels, Ty)
    """
    def __init__(self, d_in, d_out, d_hidden):
        super(PostNet, self).__init__()
        self.nets = nn.Sequential(
            nn.Upsample(scale_factor=2, mode='nearest'),
            mm.ResidualBlock1d(d_in, d_hidden, ksize=5),
            nn.Upsample(scale_factor=2, mode='nearest'),
            mm.ResidualBlock1d(d_hidden, d_hidden, ksize=5),
            mm.ResidualBlock1d(d_hidden, d_out, ksize=5)
        )

    def forward(self, x):
        y = self.nets(x)
        return torch.sigmoid(y)
