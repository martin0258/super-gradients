import abc
import random
from typing import Tuple, List, Mapping, Any, Union

import numpy as np
import torch
from torch.utils.data.dataloader import default_collate, Dataset

from super_gradients.common.abstractions.abstract_logger import get_logger
from super_gradients.common.object_names import Processings
from super_gradients.common.registry.registry import register_collate_function
from super_gradients.module_interfaces import HasPreprocessingParams
from super_gradients.training.datasets.pose_estimation_datasets.target_generators import KeypointsTargetsGenerator
from super_gradients.training.transforms.keypoint_transforms import KeypointsCompose, KeypointTransform, PoseEstimationSample
from super_gradients.training.utils.visualization.utils import generate_color_mapping

logger = get_logger(__name__)


class BaseKeypointsDataset(Dataset, HasPreprocessingParams):
    """
    Base class for pose estimation datasets.
    Descendants should implement the load_sample method to read a sample from the disk and return (image, mask, joints, extras) tuple.
    """

    def __init__(
        self,
        target_generator: KeypointsTargetsGenerator,
        transforms: List[KeypointTransform],
        min_instance_area: float,
        num_joints: int,
        edge_links: Union[List[Tuple[int, int]], np.ndarray],
        edge_colors: Union[List[Tuple[int, int, int]], np.ndarray, None],
        keypoint_colors: Union[List[Tuple[int, int, int]], np.ndarray, None],
    ):
        """

        :param target_generator: Target generator that will be used to generate the targets for the model.
            See DEKRTargetsGenerator for an example.
        :param transforms: Transforms to be applied to the image & keypoints
        :param min_instance_area: Minimum area of an instance to be included in the dataset
        :param num_joints: Number of joints to be predicted
        :param edge_links: Edge links between joints
        :param edge_colors: Color of the edge links. If None, the color will be generated randomly.
        :param keypoint_colors: Color of the keypoints. If None, the color will be generated randomly.
        """
        super().__init__()
        self.target_generator = target_generator
        self.transforms = KeypointsCompose(transforms)
        self.min_instance_area = min_instance_area
        self.num_joints = num_joints
        self.edge_links = edge_links
        self.edge_colors = edge_colors or generate_color_mapping(len(edge_links))
        self.keypoint_colors = keypoint_colors or generate_color_mapping(num_joints)

    @abc.abstractmethod
    def __len__(self) -> int:
        raise NotImplementedError()

    @abc.abstractmethod
    def load_sample(self, index) -> PoseEstimationSample:
        """
        Read a sample from the disk and return (image, mask, joints, extras) tuple
        :param index: Sample index
        :return: Tuple of (image, mask, joints, extras)
            image - Numpy array of [H,W,3] shape, which represents input RGB image
            mask - Numpy array of [H,W] shape, which represents a binary mask with zero values corresponding to an
                    ignored region which should not be used for training (contribute to loss)
            joints - Numpy array of [Num Instances, Num Joints, 3] shape, which represents the skeletons of the instances
            extras - Dictionary of extra information about the sample that should be included in `extras` dictionary.
        """
        raise NotImplementedError()

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, Any, Mapping[str, Any]]:
        sample = self.load_sample(index)
        sample = self.apply_transforms(sample, self.transforms.transforms)
        sample = self.filter_joints(sample)

        targets = self.target_generator(sample)
        return sample.image, targets, {"gt_joints": sample.joints, "gt_bboxes": sample.bboxes, "gt_areas": sample.areas, "gt_is_crowd": sample.is_crowd}

    def apply_transforms(self, sample: PoseEstimationSample, transforms: List[KeypointTransform]) -> PoseEstimationSample:
        applied_transforms_so_far = []
        for t in transforms:
            if t.additional_samples_count == 0:
                sample = t(sample)
                applied_transforms_so_far.append(t)
            else:
                additional_samples = [self.load_sample(index) for index in random.sample(range(len(self)), t.additional_samples_count)]
                additional_samples = [self.apply_transforms(sample, applied_transforms_so_far) for sample in additional_samples]
                sample.additional_samples = additional_samples
                sample = t(sample)
        return sample

    def compute_area(self, joints: np.ndarray) -> np.ndarray:
        """
        Compute area of a bounding box for each instance.
        :param joints:  [Num Instances, Num Joints, 3]
        :return: [Num Instances]
        """
        w = np.max(joints[:, :, 0], axis=-1) - np.min(joints[:, :, 0], axis=-1)
        h = np.max(joints[:, :, 1], axis=-1) - np.min(joints[:, :, 1], axis=-1)
        return w * h

    def filter_joints(self, sample: PoseEstimationSample) -> PoseEstimationSample:
        """
        Filter instances that are either too small or do not have visible keypoints
        :param joints: Array of shape [Num Instances, Num Joints, 3]
        :param image:
        :return: [New Num Instances, Num Joints, 3], New Num Instances <= Num Instances
        """
        if torch.is_tensor(sample.image):
            _, rows, cols = sample.image.shape
        else:
            rows, cols, _ = sample.image.shape

        # Update visibility of joints for those that are outside the image
        outside_image_mask = (sample.joints[:, :, 0] < 0) | (sample.joints[:, :, 1] < 0) | (sample.joints[:, :, 0] >= cols) | (sample.joints[:, :, 1] >= rows)
        sample.joints[outside_image_mask, 2] = 0

        # Filter instances with all invisible keypoints
        visible_joints_mask = sample.joints[:, :, 2] > 0
        keep_mask = np.sum(visible_joints_mask, axis=-1) > 0

        # Filter instances with too small area
        if self.min_instance_area > 0:
            if sample.areas is None:
                areas = self.compute_area(sample.joints)
            else:
                areas = sample.areas

            keep_area_mask = areas > self.min_instance_area
            keep_mask &= keep_area_mask

        sample.joints = sample.joints[keep_mask]
        sample.is_crowd = sample.is_crowd[keep_mask]
        if sample.bboxes is not None:
            sample.bboxes = sample.bboxes[keep_mask]
        if sample.areas is not None:
            sample.areas = sample.areas[keep_mask]
        return sample

    def get_dataset_preprocessing_params(self):
        """

        :return:
        """
        pipeline = self.transforms.get_equivalent_preprocessing()
        params = dict(
            conf=0.05,
            image_processor={Processings.ComposeProcessing: {"processings": pipeline}},
            edge_links=self.edge_links,
            edge_colors=self.edge_colors,
            keypoint_colors=self.keypoint_colors,
        )
        return params


@register_collate_function()
class KeypointsCollate:
    """
    Collate image & targets, return extras as is.
    """

    def __call__(self, batch):
        images = []
        targets = []
        extras = []
        for image, target, extra in batch:
            images.append(image)
            targets.append(target)
            extras.append(extra)

        extras = {k: [dic[k] for dic in extras] for k in extras[0]}  # Convert list of dicts to dict of lists

        images = default_collate(images)
        targets = default_collate(targets)
        return images, targets, extras
