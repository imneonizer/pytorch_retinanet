import importlib
import math
from typing import *
import torch
from torch import flatten
from torch.functional import Tensor

from .config import *


def collate_fn(batch):
    "`collate_fn` for pytorch obj_detection dataloader"
    return tuple(zip(*batch))


def ifnone(a: Any, b: Any) -> Any:
    """`a` if `a` is not None, otherwise `b`"""
    if a is not None:
        return a
    else:
        return b


# https://github.com/quantumblacklabs/kedro/blob/9809bd7ca0556531fa4a2fc02d5b2dc26cf8fa97/kedro/utils.py
def load_obj(obj_path: str, default_obj_path: str = "") -> Any:
    """Extract an object from a given path.
        Args:
            obj_path: Path to an object to be extracted, including the object name.
            default_obj_path: Default object path.
        Returns:
            Extracted object.
        Raises:
            AttributeError: When the object does not have the given named attribute.
    """
    obj_path_list = obj_path.rsplit(".", 1)
    obj_path      = obj_path_list.pop(0) if len(obj_path_list) > 1 else default_obj_path
    obj_name      = obj_path_list[0]
    module_obj    = importlib.import_module(obj_path)
    if not hasattr(module_obj, obj_name):
        raise AttributeError(f"Object `{obj_name}` cannot be loaded from `{obj_path}`.")
    return getattr(module_obj, obj_name)


def bbox_2_activ(bboxes: Tensor, anchors: Tensor) -> Tensor:
    """
    Convert `ground_truths` to match the model `activations` to calculate `loss`.
    """
    if anchors.device != bboxes.device:
        anchors.to(bboxes.device)
        
    # convert anchors & targets from tlbr to cthw
    a_centers = (anchors[:,:2] + anchors[:,2:])/2
    a_sizes   = anchors[:,2:] - anchors[:,:2]

    b_centers = (bboxes[:,:2] + bboxes[:,2:])/2
    b_sizes   = bboxes[:, 2:] - bboxes[:, :2]

    anchors , bboxes  = torch.cat([a_centers, a_sizes], 1), torch.cat([b_centers, b_sizes], 1)

    # Calculate Offsets
    t_centers = (bboxes[...,:2] - anchors[...,:2]) / anchors[...,2:] 
    t_sizes   = torch.log(bboxes[...,2:] / anchors[...,2:] + 1e-8) 
    return torch.cat([t_centers, t_sizes], -1).div_(bboxes.new_tensor([BBOX_REG_WEIGHTS]))

def activ_2_bbox(activations: Tensor, anchors: Tensor):
    "Converts the `activations` of the `model` to bounding boxes."
    # Gather in the same device
    if anchors.device != activations.device:
        anchors = anchors.to(activations.device)
    # Convert anchors from tlbr to cthw
    a_centers = (anchors[:,:2] + anchors[:,2:])/2
    a_sizes   = anchors[:,2:]  - anchors[:,:2]
    anchors   = torch.cat([a_centers, a_sizes], 1)

    activations.mul_(activations.new_tensor([BBOX_REG_WEIGHTS])) # multiply activation with weights
    centers = anchors[...,2:] * activations[...,:2] + anchors[...,:2] # calculate x,y center offsets
    sizes   = anchors[...,2:] * torch.exp(activations[...,:2])  # calcualte height & width
    boxes   = torch.cat([centers, sizes], -1)
    
    # Convert bbox shape from cthw to tlbr
    top_left  = boxes[:,:2] - boxes[:,2:]/2
    bot_right = boxes[:,:2] + boxes[:,2:]/2
    
    return torch.cat([top_left, bot_right], 1)

def matcher(anchors: Tensor, targets: Tensor, match_thr: float = None, back_thr: float = None):
    """
    Match `anchors` to targets. -1 is match to background, -2 is ignore.
    """
    # From: https: // github.com/fastai/course-v3/blob/9c83dfbf9b9415456c9801d299d86e099b36c86d/nbs/dl2/pascal.ipynb
    # - for each anchor we take the maximum overlap possible with any of the targets.
    # - if that maximum overlap is less than 0.4, we match the anchor box to background,
    # the classifier's target will be that class.
    # - if the maximum overlap is greater than 0.5, we match the anchor box to that ground truth object.
    # The classifier's target will be the category of that target.
    # - if the maximum overlap is between 0.4 and 0.5, we ignore that anchor in our loss computation.
    match_thr = ifnone(match_thr, IOU_THRESHOLDS_FOREGROUND)
    back_thr  = ifnone(back_thr, IOU_THRESHOLDS_BACKGROUND)
    assert (match_thr > back_thr)

    matches = anchors.new(anchors.size(0)).zero_().long() - 2
    if targets.numel() == 0:
        return matches

    # Calculate IOU between given targets & anchors
    iou_vals   = compute_IOU(anchors, targets)
    # Grab the best ground_truth overlap
    vals, idxs = iou_vals.max(dim=1)
    # Grab the idxs
    matches[vals < back_thr] = -1
    matches[vals > match_thr] = idxs[vals > match_thr]
    return matches


def compute_IOU(anchors, targets):
    "Compute the IoU values of `anchors` by `targets`."
    inter          = intersection(anchors, targets)
    anc_sz, tgt_sz = anchors[:, 2] * anchors[:, 3], targets[:, 2] * targets[:, 3]
    union          = anc_sz.unsqueeze(1) + tgt_sz.unsqueeze(0) - inter
    return inter / (union + 1e-8)


def intersection(anchors, targets):
    "Compute the sizes of the intersections of `anchors` by `targets`."
    a, t       = anchors.size(0), targets.size(0)
    ancs, tgts = (
        anchors.unsqueeze(1).expand(a, t, 4),
        targets.unsqueeze(0).expand(a, t, 4),
    )
    top_left_i  = torch.max(ancs[..., :2], tgts[..., :2])
    bot_right_i = torch.min(ancs[..., 2:], tgts[..., 2:])
    sizes       = torch.clamp(bot_right_i - top_left_i, min=0)
    return sizes[..., 0] * sizes[..., 1]

