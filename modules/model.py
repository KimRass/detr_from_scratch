# References
    # https://github.com/facebookresearch/detr/blob/main/models/matcher.py
    # https://github.com/facebookresearch/detr/blob/main/models/transformer.py

import scipy.optimize
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import resnet50
import numpy as np
import scipy
import einops

from torchvision.models import resnet50, ResNet50_Weights
from transformer import TransformerEncoder, TransformerDecoder
from iou import GIoULoss


class Backbone(nn.Module):
    """
    ". Starting from the initial image ximg ∈ R3×H0 ×W0 (with 3 color channels2), a conventional CNN backbone generates a lower-resolution activation map f ∈ RC×H×W . Typical values we use are C = 2048 and W = H 32 0 , W0 32 . H,
    """
    def __init__(self):
        super().__init__()

        self.cnn = resnet50(weights=ResNet50_Weights.DEFAULT)
        self.cnn.avgpool = nn.Identity()
        self.cnn.fc = nn.Identity()

    def forward(self, x):
        return self.cnn(x)


class DETR(nn.Module):
    def __init__(
        self,
        num_query_slots=100,
        num_classes=80,
        width=512,
        num_encoder_heads=8,
        num_decoder_heads=8,
        num_encoder_layers=6,
        num_decoder_layers=6,
        img_size=512,
        stride=32,
        feat_dim=2048,
    ):
        """
        "All transformer weights are initialized with Xavier init."
        "The backbone is with ImageNet-pretrained ResNet model from `torchvision`
        with frozen batchnorm layers. We report results with two different
        backbones: a ResNet-50 and a ResNet-101. The corresponding models are
        called respectively DETR and DETR-R101. we also increase the feature
        resolution by adding a dilation to the last stage of the backbone and
        removing a stride from the first convolution of this stage. The
        corresponding models are called respectively DETR-DC5 and DETR-DC5-R101
        (dilated C5 stage). This modification increases the resolution by a
        factor of two, thus improving performance for small objects, at the cost
        of a 16x higher cost in the self-attentions of the encoder, leading to
        an overall 2x increase in computational cost."
        
        "A 1x1 convolution reduces the channel dimension of the high-level activation map f from C to a smaller dimension d. creating a new feature map z0 ∈ Rd×H×W . The encoder expects a sequence as input, hence we collapse the spatial dimensions of z0 into one dimension, resulting in a d×HW feature map.

        The final prediction is com- puted by a 3-layer perceptron with ReLU activation function and hidden dimen- sion d, and a linear projection layer. The FFN predicts the normalized center coordinates, height and width of the box w.r.t. the input image, and the lin- ear layer predicts the class label using a softmax function.
        N is usually much larger than the actual number of objects of interest in an image, an additional special class la- bel ∅ is used to represent that no object is detected within a slot. This class plays a similar role to the “background” class in the standard object detection approaches
        TODO "We add prediction FFNs and Hungarian loss after each decoder layer. All predictions FFNs share their parameters. We use an additional shared layer-norm to normalize the input to the prediction FFNs from different decoder layers.
        "All models were trained with $N = 100$ decoder query slots."
        """
        super().__init__()

        self.num_query_slots = num_query_slots
        self.img_size = img_size
        self.stride = stride
        self.feat_dim = feat_dim

        self.backbone = Backbone()
        self.giou_loss = GIoULoss()
        self.conv = nn.Conv2d(feat_dim, width, 1, 1, 0)
        self.encoder = TransformerEncoder(
            num_heads=num_encoder_heads,
            num_layers=num_encoder_layers,
            width=width,
        )
        self.decoder = TransformerDecoder(
            num_heads=num_decoder_heads,
            num_layers=num_decoder_layers,
            width=width,
        )
        self.obj_query = nn.Embedding(num_query_slots, width).weight
        self.bbox_ffn = nn.Sequential(
            nn.Linear(width, width),
            nn.ReLU(),
            nn.Linear(width, width),
            nn.ReLU(),
            nn.Linear(width, 4),
            nn.Sigmoid(),
        )
        self.cls_ffn = nn.Sequential(
            nn.Linear(width, num_classes),
            nn.Softmax(dim=-1),
        )

    def forward(self, image):
        x = self.backbone(image)
        x = x.view(
            x.size(0),
            self.feat_dim,
            self.img_size // self.stride,
            self.img_size // self.stride,
        )
        x = self.conv(x)
        x = einops.rearrange(x, pattern="b l h w -> b (h w) l")
        enc_out = self.encoder(x)
        x = self.decoder(
            einops.repeat(self.obj_query, pattern="n d -> b n d", b=image.size(0)),
            enc_out,
        )

        pred_bbox = self.bbox_ffn(x)
        pred_prob = self.cls_ffn(x)
        return pred_bbox, pred_prob

    def get_loss(
        self,
        image,
        labels,
        gt_bboxes,
        no_obj_weight=0.1,
        l1_weight=5,
        giou_weight=2,
    ):
        """
        "Lmatch (yi , ŷσ(i) ) is a pair-wise matching cost between ground truth yi and a prediction with index σ(i). This optimal assignment is computed efficiently with the Hungarian algorithm, following prior work (e.g. [43]). The matching cost takes into account both the class prediction and the sim- ilarity of predicted and ground truth boxes. Each element i of the ground truth set can be seen as a yi = (ci , bi ) where ci is the target class label (which may be ∅) and bi ∈ [0, 1]4 is a vector that defines ground truth box cen- ter coordinates and its height and width relative to the image size. For the prediction with index σ(i) we define probability of class ci as p̂σ(i) (ci ) and the predicted box as b̂σ(i) . With these notations we define $\mathcal{L}_{\text{match}}(y_{i}, \hat{y}_{\sigma(i)})$ as
        $-\mathbb{1}_{\{c_{i} \neq \phi\}}\hat{p}_{\sigma(i)}(c_{i}) +
        \mathbb{1}_{\{c_{i} \neq \phi\}}\mathcal{L}_{\text{box}}(b_{i}, \hat{b}_{\sigma(i)})$.
        "We use linear combination of $\mathcal{l}$ and GIoU losses for
        bounding box regression with $\lambda_{L1} = 5$ and
        $\lambda_{\text{iou}} = 2$ weights respectively."
        """
        batched_pred_bbox, batched_pred_prob = self(image)

        sum_losses = torch.zeros((1,), dtype=torch.float32)
        for pred_bbox, pred_prob, label, gt_bbox in zip(
            batched_pred_bbox,
            batched_pred_prob,
            labels,
            gt_bboxes
        ):
            giou = self.giou_loss(pred_bbox, gt_bbox)
            label_prob = pred_prob[:, label]
            match_loss = -label_prob + giou
            pred_indices, gt_indices = scipy.optimize.linear_sum_assignment(
                match_loss.detach().cpu().numpy(),
            )

            loss = no_obj_weight * torch.sum(
                -torch.log(label_prob[pred_indices, gt_indices])
            )
            loss += giou_weight * torch.sum(giou[pred_indices, gt_indices])
            loss += l1_weight * torch.sum(
                torch.abs(pred_bbox[pred_indices] - gt_bbox[gt_indices])
            )
            sum_losses += loss
        sum_losses /= (image.size(0) * self.num_query_slots)
        return sum_losses


if __name__ == "__main__":
    import random

    batch_size = 4
    num_query_slots = 40
    num_classes = 80

    num_objs = [random.randint(0, 20) for _ in range(batch_size)]
    labels = [torch.randint(0, num_classes, size=(i,)) for i in num_objs]
    gt_bboxes = [torch.rand((i, 4)) for i in num_objs]

    model = DETR()
    image = torch.randn((4, 3, 512, 512))
    loss = model.get_loss(
        image=image, labels=labels, gt_bboxes=gt_bboxes,
    )
    print(loss)


    # # pred_orders = [random.sample(range(i), i) for i in num_objs]
    # pred_cls_logits = torch.rand((batch_size, num_query_slots, num_classes))
    # pred_bboxes = torch.rand((batch_size, num_query_slots, 4))
    # pred_bboxes.shape

    # batch_idx = 0
    # label = labels[batch_idx] # "$c_{i}$"
    # gt_bbox = gt_bboxes[batch_idx] # "$b_{i}$"
    # pred_bbox = pred_bboxes[batch_idx]
    # pred_order = pred_orders[batch_idx] # "$\sigma(i)$"
    # pred_cls_logit = pred_cls_logits[batch_idx]
    
