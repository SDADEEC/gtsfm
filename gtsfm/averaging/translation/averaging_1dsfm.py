"""Translation averaging using 1DSFM.

This algorithm was proposed in `Robust Global Translations with 1DSFM' and is implemented by wrapping GTSAM's classes.

References:
- https://research.cs.cornell.edu/1dsfm/
- https://github.com/borglab/gtsam/blob/develop/gtsam/sfm/MFAS.h
- https://github.com/borglab/gtsam/blob/develop/gtsam/sfm/TranslationRecovery.h
- https://github.com/borglab/gtsam/blob/develop/python/gtsam/examples/TranslationAveragingExample.py

Authors: Jing Wu, Ayush Baid, Akshay Krishnan
"""
from collections import defaultdict
from enum import Enum
from typing import DefaultDict, Dict, List, Optional, Set, Tuple

import gtsam
import numpy as np
from gtsam import (
    BinaryMeasurementsPoint3,
    BinaryMeasurementPoint3,
    BinaryMeasurementsUnit3,
    BinaryMeasurementUnit3,
    MFAS,
    Point3,
    Pose3,
    Rot3,
    symbol_shorthand,
    TranslationRecovery,
    Unit3,
)

import gtsfm.common.types as gtsfm_types
import gtsfm.utils.geometry_comparisons as comp_utils
import gtsfm.utils.logger as logger_utils
import gtsfm.utils.metrics as metrics_utils
import gtsfm.utils.sampling as sampling_utils
from gtsfm.averaging.translation.translation_averaging_base import TranslationAveragingBase
from gtsfm.common.pose_prior import PosePrior
from gtsfm.common.sfm_track import SfmTrack2d
from gtsfm.evaluation.metrics import GtsfmMetric, GtsfmMetricsGroup

# Hyperparameters for 1D-SFM
# maximum number of times 1dsfm will project the Unit3's to a 1d subspace for outlier rejection
MAX_PROJECTION_DIRECTIONS = 2000
OUTLIER_WEIGHT_THRESHOLD = 0.125

NOISE_MODEL_DIMENSION = 3  # chordal distances on Unit3
NOISE_MODEL_SIGMA = 0.01
HUBER_LOSS_K = 1.345  # default value from GTSAM

MAX_INLIER_MEASUREMENT_ERROR_DEG = 5.0

TRACKS_TO_CAMERAS_RATIO = 20

logger = logger_utils.get_logger()

C = symbol_shorthand.A
L = symbol_shorthand.B

RelativeDirectionsDict = Dict[Tuple[int, int], Unit3]


class TranslationAveraging1DSFM(TranslationAveragingBase):
    """1D-SFM translation averaging with outlier rejection."""

    class ProjectionSamplingMethod(str, Enum):
        """Used to select how the projection directions in 1DSfM are sampled."""

        # The string values for enums enable using them in the config.

        # Randomly choose projection directions from input measurements.
        SAMPLE_INPUT_MEASUREMENTS = "SAMPLE_INPUT_MEASUREMENTS"
        # Fit a Gaussian density to input measurements and sample from it.
        SAMPLE_WITH_INPUT_DENSITY = "SAMPLE_WITH_INPUT_DENSITY"
        # Uniformly sample 3D directions at random.
        SAMPLE_WITH_UNIFORM_DENSITY = "SAMPLE_WITH_UNIFORM_DENSITY"

    def __init__(
        self,
        robust_measurement_noise: bool = True,
        use_tracks_for_averaging: bool = True,
        reject_outliers: bool = True,
        projection_sampling_method: ProjectionSamplingMethod = ProjectionSamplingMethod.SAMPLE_WITH_UNIFORM_DENSITY,
    ) -> None:
        """Initializes the 1DSFM averaging instance.

        Args:
            robust_measurement_noise: Whether to use a robust noise model for the measurements, defaults to true.
            reject_outliers: whether to perform outlier rejection with MFAS algorithm (default True).
            projection_sampling_method: ProjectionSamplingMethod to be used for directions to run 1DSfM.
        """
        super().__init__(robust_measurement_noise)

        self._max_1dsfm_projection_directions = MAX_PROJECTION_DIRECTIONS
        self._outlier_weight_threshold = OUTLIER_WEIGHT_THRESHOLD
        self._reject_outliers = reject_outliers
        self._projection_sampling_method = projection_sampling_method
        self._use_tracks_for_averaging = use_tracks_for_averaging

    def __sample_projection_directions(
        self,
        w_i2Ui1_list: List[Unit3],
    ) -> List[Unit3]:
        """Samples projection directions for 1DSfM based on the provided sampling method.

        Args:
            w_i2Ui1_list: List of unit translations to be used for biasing sampling.
            Used only if the sampling method is SAMPLE_INPUT_MEASUREMENTS or SAMPLE_WitH_INPUT_DENSITY.

        Returns:
            List of sampled Unit3 projection directions.
        """
        num_measurements = len(w_i2Ui1_list)

        if self._projection_sampling_method == self.ProjectionSamplingMethod.SAMPLE_INPUT_MEASUREMENTS:
            num_samples = min(num_measurements, self._max_1dsfm_projection_directions)
            sampled_indices = np.random.choice(num_measurements, num_samples, replace=False)
            projections = [w_i2Ui1_list[idx] for idx in sampled_indices]
        elif self._projection_sampling_method == self.ProjectionSamplingMethod.SAMPLE_WITH_INPUT_DENSITY:
            projections = sampling_utils.sample_kde_directions(
                w_i2Ui1_list, num_samples=self._max_1dsfm_projection_directions
            )
        elif self._projection_sampling_method == self.ProjectionSamplingMethod.SAMPLE_WITH_UNIFORM_DENSITY:
            projections = sampling_utils.sample_random_directions(num_samples=self._max_1dsfm_projection_directions)
        else:
            raise ValueError("Unsupported sampling method!")

        return projections

    def _binary_measurements_from_dict(
        self,
        w_i2Ui1_dict: RelativeDirectionsDict,
        w_i2Ui1_dict_tracks: RelativeDirectionsDict,
        noise_model: gtsam.noiseModel,
    ) -> BinaryMeasurementsUnit3:
        """Gets a list of BinaryMeasurementUnit3 by combining measurements in w_i2Ui1_dict and w_i2Ui1_dict_tracks.

        Args:
            w_i2Ui1_dict: Dictionary of Unit3 relative translations between cameras.
            w_i2Ui1_dict_tracks: Dictionary of Unit3 relative translations between cameras and landmarks.
            noise_model: Noise model to use for the measurements.

        Returns:
            List of binary measurements.
        """
        w_i1Ui2_measurements = BinaryMeasurementsUnit3()
        for (i1, i2), w_i2Ui1 in w_i2Ui1_dict.items():
            w_i1Ui2_measurements.append(BinaryMeasurementUnit3(C(i2), C(i1), w_i2Ui1, noise_model))
        for (track_id, cam_id), w_i2Ui1 in w_i2Ui1_dict_tracks.items():
            w_i1Ui2_measurements.append(BinaryMeasurementUnit3(C(cam_id), L(track_id), w_i2Ui1, noise_model))

        return w_i1Ui2_measurements

    def _binary_measurements_from_priors(
        self, i2Ti1_priors: Dict[Tuple[int, int], PosePrior], wRi_list: List[Rot3]
    ) -> BinaryMeasurementsPoint3:
        """Converts the priors from relative Pose3 priors to relative Point3 measurements in world frame.

        Args:
            i2Ti1_priors: Relative pose priors between cameras, could be a hard or soft prior.
            wRi_list: Absolute rotation estimates from rotation averaging.

        Returns:
            BinaryMeasurementsPoint3 containing Point3 priors in world frame.
        """

        def get_prior_in_world_frame(i2, i2Ti1_prior):
            return wRi_list[i2].rotate(i2Ti1_prior.value.translation())

        w_i1ti2_prior_measurements = BinaryMeasurementsPoint3()
        if len(i2Ti1_priors) == 0:
            return w_i1ti2_prior_measurements

        # TODO(akshay-krishnan): Use the translation covariance, transform to world frame.
        # noise_model = gtsam.noiseModel.Gaussian.Covariance(i2Ti1_prior.covariance)
        # TODO(akshay-krishnan): use robust noise model for priors?
        noise_model = gtsam.noiseModel.Isotropic.Sigma(3, 1e-2)
        for (i1, i2), i2Ti1_prior in i2Ti1_priors.items():
            w_i1ti2_prior_measurements.append(
                BinaryMeasurementPoint3(
                    C(i2),
                    C(i1),
                    get_prior_in_world_frame(i2, i2Ti1_prior),
                    noise_model,
                )
            )
        return w_i1ti2_prior_measurements

    def compute_inliers(
        self,
        w_i2Ui1_dict: RelativeDirectionsDict,
        w_i2Ui1_dict_tracks: RelativeDirectionsDict,
    ) -> Tuple[RelativeDirectionsDict, RelativeDirectionsDict, Set[int]]:
        """Perform inlier detection for the relative direction measurements.

        Args:
            w_i2Ui1_dict: Dictionary of Unit3 relative translations between cameras.
            w_i2Ui1_dict_tracks: Dictionary of Unit3 relative translations between cameras and landmarks.

        Returns:
            Set of indices (i1, i2) which are inliers.
        """

        # Sample directions for projection
        combined_measurements = list(w_i2Ui1_dict.values()) + list(w_i2Ui1_dict_tracks.values())
        projection_directions = self.__sample_projection_directions(combined_measurements)

        # Convert to measurements - indexes to symbols.
        dummy_noise_model = gtsam.noiseModel.Isotropic.Sigma(3, 1e-2)  # MFAS does not use this.
        w_i1Ui2_measurements = self._binary_measurements_from_dict(w_i2Ui1_dict, w_i2Ui1_dict_tracks, dummy_noise_model)

        # Compute outlier weights using MFAS.
        # TODO(ayush): parallelize this step.
        outlier_weights: List[Dict[Tuple[int, int], float]] = []
        for direction in projection_directions:
            mfas_instance = MFAS(w_i1Ui2_measurements, direction)
            outlier_weights.append(mfas_instance.computeOutlierWeights())
        logger.debug("Computed outlier weights using MFAS.")

        # Compute average outlier weight.
        outlier_weights_sum: DefaultDict[Tuple[int, int], float] = defaultdict(float)
        inliers = set()
        for outlier_weight_dict in outlier_weights:
            for w_i1Ui2 in w_i1Ui2_measurements:
                i1, i2 = w_i1Ui2.key1(), w_i1Ui2.key2()
                outlier_weights_sum[(i1, i2)] += outlier_weight_dict[(i1, i2)]
        for (i1, i2), weight_sum in outlier_weights_sum.items():
            if weight_sum / len(projection_directions) < OUTLIER_WEIGHT_THRESHOLD:
                inliers.add((i1, i2))

        # Filter outliers, index back from symbol to int.
        inlier_w_i2Ui1_dict = {}
        inlier_w_i2Ui1_dict_tracks = {}
        inlier_cameras: Set[int] = set()
        for (i1, i2) in w_i2Ui1_dict:
            if (C(i2), C(i1)) in inliers:  # there is a flip in indices from w_i2Ui1_dict to inliers.
                inlier_w_i2Ui1_dict[(i1, i2)] = w_i2Ui1_dict[(i1, i2)]
                inlier_cameras.add(i1)
                inlier_cameras.add(i2)

        for (track_id, cam_id) in w_i2Ui1_dict_tracks:
            if (C(cam_id), L(track_id)) in inliers and cam_id in inlier_cameras:
                inlier_w_i2Ui1_dict_tracks[(track_id, cam_id)] = w_i2Ui1_dict_tracks[(track_id, cam_id)]

        return inlier_w_i2Ui1_dict, inlier_w_i2Ui1_dict_tracks, inlier_cameras

    def __get_initial_values(self, wTi_initial: List[Optional[PosePrior]]) -> gtsam.Values:
        """Converts translations from a list of absolute poses to gtsam.Values for initialization.

        Args:
            wTi_initial: List of absolute poses.

        Returns:
            gtsam.Values containing initial translations (uses symbols for camera index).
        """
        initial = gtsam.Values()
        for i, wTi in enumerate(wTi_initial):
            if wTi is not None:
                initial.insertPoint3(C(i), wTi.value.translation())
        return initial

    def _select_tracks_for_averaging(
        self,
        tracks: List[SfmTrack2d],
        valid_cameras: Set[int],
        intrinsics: List[Optional[gtsfm_types.CALIBRATION_TYPE]],
        tracks_to_cameras_ratio: float = TRACKS_TO_CAMERAS_RATIO,
    ) -> List[SfmTrack2d]:
        """Removes bad tracks and returns the longest ones from the rest based on tracks_to_cameras_ratio.

        Bad tracks are those that have fewer than 3 measurements from valid_cameras.
        Sorts the remaining tracks in descending order by number of measurements, and returns as many tracks
        as tracks_to_cameras_ratio * number of valid cameras.

        Args:
            tracks: List of all input tracks.
            valid_cameras: Set of valid camera indices (these have direction measurements and valid rotations).
            intrinsics: List of camera intrinsics.
            tracks_to_cameras_ratio: Ratio of tracks to cameras to use for averaging.

        Returns:
            List of tracks to use for averaging.
        """
        max_tracks = int(len(valid_cameras) * tracks_to_cameras_ratio)
        filtered_tracks = []
        valid_cameras_with_intrinsics = set([c for c in valid_cameras if intrinsics[c] is not None])
        for track in tracks:
            valid_cameras_track = track.select_for_cameras(camera_idxs=valid_cameras_with_intrinsics)
            if valid_cameras_track.number_measurements() < 3:
                continue
            filtered_tracks.append(valid_cameras_track)
        return sorted(filtered_tracks, key=lambda track: track.number_measurements(), reverse=True)[:max_tracks]

    def _get_landmark_directions(
        self,
        tracks_2d: List[SfmTrack2d],
        intrinsics: List[Optional[gtsfm_types.CALIBRATION_TYPE]],
        wRi_list: List[Optional[Rot3]],
    ) -> RelativeDirectionsDict:
        """Computes the camera to landmark directions for each track, in world frame.

        Args:
            tracks_2d: 2d tracks in each image, assuming that all measurements in all tracks are for valid cameras.
            intrinsics: camera intrinsics for each camera.
            wRi_list: camera rotations in world frame.

        Returns:
            Dictionary of unit directions from camera to track in world frame indexed by (track_id, camera_id).
        """
        landmark_directions = {}
        for track_id, track in enumerate(tracks_2d):
            for j in range(track.number_measurements()):
                measurement = track.measurement(j)
                cam_idx = measurement.i

                if intrinsics[cam_idx] is None or wRi_list[cam_idx] is None:
                    raise ValueError("Camera intrinsics or rotation cannot be None for input track measurements")

                measurement_xy = intrinsics[cam_idx].calibrate(measurement.uv)
                measurement_homog = Point3(measurement_xy[0], measurement_xy[1], 1.0)
                w_cam_U_track = Unit3(wRi_list[cam_idx].rotate(Unit3(measurement_homog).point3()))

                landmark_directions[(track_id, cam_idx)] = w_cam_U_track  # w_i2Ui1 here
        return landmark_directions

    def __run_averaging(
        self,
        num_images: int,
        w_i2Ui1_dict: RelativeDirectionsDict,
        w_i2Ui1_dict_tracks: RelativeDirectionsDict,
        wRi_list,
        i2Ti1_priors,
        absolute_pose_priors,
        scale_factor,
    ):
        """Runs the averaging algorithm.

        Args:
            w_i2Ui1_dict: Relative translations between cameras in world frame.
            wTi_initial: Initial translations for each camera in world frame.

        Returns:
            List of camera poses in world frame.
        """
        logger.info(
            "Using {} track measurements and {} camera measurements".format(len(w_i2Ui1_dict_tracks), len(w_i2Ui1_dict))
        )

        noise_model = gtsam.noiseModel.Isotropic.Sigma(NOISE_MODEL_DIMENSION, NOISE_MODEL_SIGMA)
        if self._robust_measurement_noise:
            huber_loss = gtsam.noiseModel.mEstimator.Huber.Create(HUBER_LOSS_K)
            noise_model = gtsam.noiseModel.Robust.Create(huber_loss, noise_model)

        w_i1Ui2_measurements = self._binary_measurements_from_dict(w_i2Ui1_dict, w_i2Ui1_dict_tracks, noise_model)

        # Run the optimizer.
        try:
            algorithm = TranslationRecovery()
            if len(i2Ti1_priors) > 0:
                # scale is ignored here.
                w_i1ti2_priors = self._binary_measurements_from_priors(i2Ti1_priors, wRi_list)
                wti_initial = self.__get_initial_values(absolute_pose_priors)
                wti_values = algorithm.run(w_i1Ui2_measurements, 0.0, w_i1ti2_priors, wti_initial)
            else:
                wti_values = algorithm.run(w_i1Ui2_measurements, scale_factor)
        except TypeError as e:
            # TODO(akshay-krishnan): remove once latest gtsam pip wheels updated.
            logger.error("TypeError: {}".format(str(e)))
            algorithm = TranslationRecovery(w_i1Ui2_measurements)
            wti_values = algorithm.run(scale_factor)

        # transforming the result to the list of Point3
        wti_list: List[Optional[Point3]] = [None] * num_images
        for i in range(num_images):
            if wRi_list[i] is not None and wti_values.exists(C(i)):
                wti_list[i] = wti_values.atPoint3(C(i))
        return wti_list

    # TODO(ayushbaid): Change wTi_initial to Pose3.
    def run_translation_averaging(
        self,
        num_images: int,
        i2Ui1_dict: Dict[Tuple[int, int], Optional[Unit3]],
        wRi_list: List[Optional[Rot3]],
        tracks_2d: List[SfmTrack2d] = [],
        intrinsics: List[Optional[gtsfm_types.CALIBRATION_TYPE]] = [],
        absolute_pose_priors: List[Optional[PosePrior]] = [],
        i2Ti1_priors: Dict[Tuple[int, int], PosePrior] = {},
        scale_factor: float = 1.0,
        gt_wTi_list: Optional[List[Optional[Pose3]]] = None,
    ) -> Tuple[List[Optional[Pose3]], Optional[GtsfmMetricsGroup]]:
        """Run the translation averaging.

        Args:
            num_images: number of camera poses.
            i2Ui1_dict: relative unit-translation as dictionary (i1, i2): i2Ui1
            wRi_list: global rotations for each camera pose in the world coordinates.
            absolute_pose_priors: priors on the camera poses (not delayed).
            i2Ti1_priors: priors on the pose between camera pairs (not delayed) as (i1, i2): i2Ti1.
            scale_factor: non-negative global scaling factor.
            gt_wTi_list: ground truth poses for computing metrics.

        Returns:
            Global translation wti for each camera pose. The number of entries in the list is `num_images`. The list
                may contain `None` where the global translations could not be computed (either underconstrained system
                or ill-constrained system).
            A GtsfmMetricsGroup of 1DSfM metrics.
        """
        logger.info("Running translation averaging on {} unit translations".format(len(i2Ui1_dict)))

        w_i2Ui1_dict, valid_cameras = get_valid_measurements_in_world_frame(i2Ui1_dict, wRi_list)

        if self._use_tracks_for_averaging:
            if len(tracks_2d) == 0:
                logger.info("No tracks provided for translation averaging. Falling back to camera unit translations.")
                w_i2Ui1_dict_tracks = {}
            elif len(intrinsics) != len(wRi_list):
                raise ValueError("Number of intrinsics must match number of rotations")
            else:
                selected_tracks = self._select_tracks_for_averaging(tracks_2d, valid_cameras, intrinsics)
                w_i2Ui1_dict_tracks = self._get_landmark_directions(selected_tracks, intrinsics, wRi_list)
        else:
            w_i2Ui1_dict_tracks = {}

        w_i2Ui1_dict_inliers, w_i2Ui1_dict_tracks_inliers, inlier_cameras = self.compute_inliers(
            w_i2Ui1_dict, w_i2Ui1_dict_tracks
        )

        wti_list = self.__run_averaging(
            num_images,
            w_i2Ui1_dict_inliers,
            w_i2Ui1_dict_tracks_inliers,
            wRi_list,
            i2Ti1_priors,
            absolute_pose_priors,
            scale_factor,
        )

        # Compute the metrics.
        if gt_wTi_list is not None:
            ta_metrics = compute_metrics(set(w_i2Ui1_dict_inliers.keys()), i2Ui1_dict, wRi_list, wti_list, gt_wTi_list)
        else:
            ta_metrics = None

        num_translations = sum([1 for wti in wti_list if wti is not None])
        logger.info("Estimated %d translations out of %d images.", num_translations, num_images)

        # Combine estimated global rotations and global translations to Pose(3) objects.
        wTi_list = [
            Pose3(wRi, wti) if wRi is not None and wti is not None else None for wRi, wti in zip(wRi_list, wti_list)
        ]
        raise ValueError("dummy error")
        return wTi_list, ta_metrics


def get_measurement_angle_errors(
    i1_i2_pairs: Set[Tuple[int, int]],
    i2Ui1_measurements: RelativeDirectionsDict,
    gt_i2Ui1_measurements: RelativeDirectionsDict,
) -> List[float]:
    """Returns a list of the angle between i2Ui1_measurements and gt_i2Ui1_measurements for every
    (i1, i2) in i1_i2_pairs.

    Args:
        i1_i2_pairs: List of (i1, i2) tuples for which the angles must be computed.
        i2Ui1_measurements: Measured translation direction of i1 WRT i2.
        gt_i2Ui1_measurements: Ground truth translation direction of i1 WRT i2.

    Returns:
        List of angles between the measured and ground truth translation directions.
    """
    errors: List[float] = []
    for (i1, i2) in i1_i2_pairs:
        if (i1, i2) in i2Ui1_measurements and (i1, i2) in gt_i2Ui1_measurements:
            error = comp_utils.compute_relative_unit_translation_angle(
                i2Ui1_measurements[(i1, i2)], gt_i2Ui1_measurements[(i1, i2)]
            )
            if error is None:
                raise ValueError("Unexpected None when computnig relative translation angle metric.")
            errors.append(error)
    return errors


def compute_metrics(
    inlier_i1_i2_pairs: Set[Tuple[int, int]],
    i2Ui1_dict: Dict[Tuple[int, int], Optional[Unit3]],
    wRi_list: List[Optional[Rot3]],
    wti_list: List[Optional[Point3]],
    gt_wTi_list: List[Optional[Pose3]],
) -> GtsfmMetricsGroup:
    """Computes the translation averaging metrics as a metrics group.
    Args:
        inlier_i1_i2_pairs: List of inlier camera pair indices.
        i2Ui1_dict: Translation directions between camera pairs (inputs to translation averaging).
        wRi_list: Estimated camera rotations from rotation averaging.
        wti_list: Estimated camera translations from translation averaging.
        gt_wTi_list: List of ground truth camera poses.
    Returns:
        Translation averaging metrics as a metrics group. Includes the following metrics:
        - Number of inlier, outlier and total measurements.
        - Distribution of translation direction angles for inlier measurements.
        - Distribution of translation direction angle for outlier measurements.
    """
    # Get ground truth translation directions for the measurements.
    gt_i2Ui1_dict = metrics_utils.get_twoview_translation_directions(gt_wTi_list)
    outlier_i1_i2_pairs = (
        set([pair_idx for pair_idx, val in i2Ui1_dict.items() if val is not None]) - inlier_i1_i2_pairs
    )

    # Angle between i2Ui1 measurement and GT i2Ui1 measurement for inliers and outliers.
    inlier_angular_errors = get_measurement_angle_errors(inlier_i1_i2_pairs, i2Ui1_dict, gt_i2Ui1_dict)
    outlier_angular_errors = get_measurement_angle_errors(outlier_i1_i2_pairs, i2Ui1_dict, gt_i2Ui1_dict)
    precision, recall = metrics_utils.get_precision_recall_from_errors(
        inlier_angular_errors, outlier_angular_errors, MAX_INLIER_MEASUREMENT_ERROR_DEG
    )

    measured_gt_i2Ui1_dict = {}
    for (i1, i2) in set.union(inlier_i1_i2_pairs, outlier_i1_i2_pairs):
        measured_gt_i2Ui1_dict[(i1, i2)] = gt_i2Ui1_dict[(i1, i2)]

    # Compute estimated poses after the averaging step and align them to ground truth.
    wTi_list: List[Optional[Pose3]] = []
    for (wRi, wti) in zip(wRi_list, wti_list):
        if wRi is None or wti is None:
            wTi_list.append(None)
        else:
            wTi_list.append(Pose3(wRi, wti))
    wTi_aligned_list, _ = comp_utils.align_poses_sim3_ignore_missing(gt_wTi_list, wTi_list)
    wti_aligned_list = [wTi.translation() if wTi is not None else None for wTi in wTi_aligned_list]
    gt_wti_list = [gt_wTi.translation() if gt_wTi is not None else None for gt_wTi in gt_wTi_list]

    num_total_measurements = len(inlier_i1_i2_pairs) + len(outlier_i1_i2_pairs)
    threshold_suffix = str(int(MAX_INLIER_MEASUREMENT_ERROR_DEG)) + "_deg"
    ta_metrics = [
        GtsfmMetric("num_total_1dsfm_measurements", num_total_measurements),
        GtsfmMetric("num_inlier_1dsfm_measurements", len(inlier_i1_i2_pairs)),
        GtsfmMetric("num_outlier_1dsfm_measurements", len(outlier_i1_i2_pairs)),
        GtsfmMetric("1dsfm_precision_" + threshold_suffix, precision),
        GtsfmMetric("1dsfm_recall_" + threshold_suffix, recall),
        GtsfmMetric("num_translations_estimated", len([wti for wti in wti_list if wti is not None])),
        GtsfmMetric("1dsfm_inlier_angular_errors_deg", inlier_angular_errors),
        GtsfmMetric("1dsfm_outlier_angular_errors_deg", outlier_angular_errors),
        metrics_utils.compute_translation_angle_metric(measured_gt_i2Ui1_dict, wTi_aligned_list),
        metrics_utils.compute_translation_distance_metric(wti_aligned_list, gt_wti_list),
    ]

    return GtsfmMetricsGroup("translation_averaging_metrics", ta_metrics)


def get_valid_measurements_in_world_frame(
    i2Ui1_dict: Dict[Tuple[int, int], Optional[Unit3]], wRi_list: List[Optional[Rot3]]
) -> Tuple[RelativeDirectionsDict, Set[int]]:
    """Returns measurements for which both cameras have valid rotations, transformed to the world frame.

    Args:
        i2Ui1_dict: Relative translation directions between camera pairs.
        wRi_list: List of estimated camera rotations.

    Returns:
        Tuple of:
            Relative translation directions between camera pairs, in world frame.
            Set of camera indices for which we have valid rotations and measurements.
    """

    w_i2Ui1_dict = {}
    valid_cameras: Set[int] = set()
    for (i1, i2), i2Ui1 in i2Ui1_dict.items():
        wRi2 = wRi_list[i2]
        if i2Ui1 is not None and wRi2 is not None:
            w_i2Ui1_dict[(i1, i2)] = Unit3(wRi2.rotate(i2Ui1.point3()))
            valid_cameras.add(i1)
            valid_cameras.add(i2)
        # should we retain a None in the dict?
    return w_i2Ui1_dict, valid_cameras
