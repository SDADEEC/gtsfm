"""Base class for the M (matcher) stage of the front end.

Authors: Ayush Baid
"""
import abc
from typing import Tuple, List

import dask
import numpy as np
from dask.delayed import Delayed

from gtsfm.common.keypoints import Keypoints

from gtsfm.ui.registry import BlueNode


class MatcherNode(BlueNode):
    def __init__(self):
        super().__init__()

    def _set_ui_metadata(self):
        self._display_name: str = "Matcher"
        self._input_gray_nodes: List[str] = ["Descriptors", "Image Pair Indices"]
        self._output_gray_nodes: List[str] = ["Putative Correspondence Indices"]
        # self._parent_plate: str = ""


class MatcherBase(metaclass=abc.ABCMeta):
    """Base class for all matchers.

    Matchers work on a pair of descriptors and match them by their distance.
    """

    @abc.abstractmethod
    def match(
        self,
        keypoints_i1: Keypoints,
        keypoints_i2: Keypoints,
        descriptors_i1: np.ndarray,
        descriptors_i2: np.ndarray,
        im_shape_i1: Tuple[int, int],
        im_shape_i2: Tuple[int, int],
    ) -> np.ndarray:
        """Match descriptor vectors.

        # Some matcher implementations (such as SuperGlue) utilize keypoint coordinates as
        # positional encoding, so our matcher API provides them for optional use.

        Output format:
        1. Each row represents a match.
        2. First column represents keypoint index from image #i1.
        3. Second column represents keypoint index from image #i2.
        4. Matches are sorted in descending order of the confidence (score), if possible.

        Args:
            keypoints_i1: keypoints for image #i1, of length N1.
            keypoints_i2: keypoints for image #i2, of length N2.
            descriptors_i1: descriptors corr. to keypoints_i1.
            descriptors_i2: descriptors corr. to keypoints_i2.
            im_shape_i1: shape of image #i1, as (height,width).
            im_shape_i2: shape of image #i2, as (height,width).


        Returns:
            Match indices (sorted by confidence), as matrix of shape (N, 2), where N < min(N1, N2).
        """
        # TODO(ayush): should I define matcher on descriptors or the distance matrices.
        # TODO(ayush): how to handle deep-matchers which might require the full image as input

    def create_computation_graph(
        self,
        keypoints_i1_graph: Delayed,
        keypoints_i2_graph: Delayed,
        descriptors_i1_graph: Delayed,
        descriptors_i2_graph: Delayed,
        im_shape_i1: Tuple[int, int],
        im_shape_i2: Tuple[int, int],
    ) -> Delayed:
        """
        Generates computation graph for matched features using description graphs.

        Args:
            keypoints_i1_graph: keypoints for image #i1, wrapped in Delayed.
            keypoints_i2_graph: keypoints for image #i2, wrapped in Delayed.
            descriptors_i1_graph: descriptors corr. to keypoints_i1.
            descriptors_i2_graph: descriptors corr. to keypoints_i2.
            im_shape_i1: (H,W) shape of image #i1.
            im_shape_i2: (H,W) shape of image #i2.

        Returns:
            Delayed dask tasks for matching for input camera pairs.
        """
        return dask.delayed(self.match)(
            keypoints_i1_graph,
            keypoints_i2_graph,
            descriptors_i1_graph,
            descriptors_i2_graph,
            im_shape_i1,
            im_shape_i2,
        )
