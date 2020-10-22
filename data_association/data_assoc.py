import abc
from collections import defaultdict
from typing import DefaultDict, Dict, List, Tuple, Optional

import dask
import numpy as np
import cv2

import gtsam
from data_association.feature_tracks import FeatureTracks

class DataAssociation(FeatureTracks):
    """
    Class to form feature tracks; for each track, call LandmarkInitialization
    """
    def __init__(
        self, 
        matches: Dict[Tuple[int, int], Tuple[int, int]], 
        num_poses: int, 
        global_poses: List[gtsam.Pose3], 
        calibrationFlag: bool, 
        calibration: gtsam.Cal3_S2, 
        camera_list: List,
        feature_list: List
        ) -> None:
        """
        #CAN NUM POSES BE REPLACED WITH LEN(POSES)?
        Args:
            matches: Dict of pairwise matches of form {(img1, img2): (features1, features2)
            num_poses: number of poses
            global poses: list of poses  
            calibrationFlag: flag to set shared or individual calibration
            calibration: shared calibration
            camera_list: list of individual cameras (if calibration not shared)
            feature_list: List of features (with their corresponding indices?)
        """
        print("received matches", matches)
        self.calibrationFlag = calibrationFlag
        self.calibration = calibration
        self.features_list = feature_list
        # self.sfmdata_landmark_map = gtsam.SfmData()
        self.triangulated_landmark_map = gtsam.SfmData()
        super().__init__(matches, num_poses, feature_list)
        self.sfmdata_landmark_map = self.filtered_landmark_data
        
        
        for track_idx in range(self.sfmdata_landmark_map.number_tracks()):
            if self.calibrationFlag == True:
                LMI = LandmarkInitialization(calibrationFlag, self.sfmdata_landmark_map.track(track_idx), calibration,global_poses)
            else:
                LMI = LandmarkInitialization(calibrationFlag, self.sfmdata_landmark_map.track(track_idx), camera_list)
            triangulated_data = LMI.triangulate(self.sfmdata_landmark_map.track(track_idx))
            filtered_track = LMI.filter_reprojection_error(triangulated_data)
            if filtered_track.number_measurements() > 2:
                self.triangulated_landmark_map.add_track(filtered_track)
            else:
                print("Track length < 3 discarded")
        
        print("old map", self.sfmdata_landmark_map.track(0).measurements())
        
        

class LandmarkInitialization(metaclass=abc.ABCMeta):
    """
    Class to initialize landmark points via triangulation
    """

    def __init__(
        self, 
    calibrationFlag: bool,
    obs_list: List,
    calibration: Optional[gtsam.Cal3_S2] = None, 
    track_poses: Optional[List[gtsam.Pose3]] = None, 
    track_cameras: Optional[List[gtsam.Cal3_S2]] = None
    ) -> None:
        """
        Args:
            calibrationFlag: check if shared calibration exists(True) or each camera has individual calibration(False)
            obs_list: Feature track of type [(img_idx, img_measurement),..]
            calibration: Shared calibration
            track_poses: List of poses in a feature track
            track_cameras: List of cameras in a feature track
        """
        self.sharedCal_Flag = calibrationFlag
        self.observation_list = obs_list
        self.calibration = calibration
        # for shared calibration
        if track_poses is not None:
            self.track_pose_list = track_poses
        # for multiple cameras with individual calibrations
        if track_cameras is not None:
            self.track_camera_list = track_cameras
    
    
    def create_landmark_map(self, filtered_map:gtsam.SfmData, triangulated_pts: List) -> Dict:
        landmark_map = filtered_map.copy()
        for idx, (key, val) in enumerate(filtered_map.items()):
            new_key = tuple(triangulated_pts[idx])
            # copy the value
            landmark_map[new_key] = filtered_map[key]
            del landmark_map[key]
        return landmark_map

    def extract_end_measurements(self, track: gtsam.SfmTrack) -> Tuple[gtsam.Pose3Vector, List, gtsam.Point2Vector]:
        """
        Extract first and last measurements in a track for triangulation.
        Args:
            track: feature track from which measurements are to be extracted
        Returns:
            pose_estimates: Poses of first and last measurements in track
            camera_list: Individual camera calibrations for first and last measurement
            img_measurements: Observations corresponding to first and last measurements
        """
        pose_estimates_track = gtsam.Pose3Vector()
        pose_estimates = gtsam.Pose3Vector()
        cameras_list_track = []
        cameras_list = []
        img_measurements_track = gtsam.Point2Vector()
        img_measurements = gtsam.Point2Vector()
        for measurement_idx in range(track.number_measurements()):
            img_idx, img_Pt = track.measurement(measurement_idx)
        # for (img_idx, img_Pt) in track:
            if self.sharedCal_Flag:
                pose_estimates_track.append(self.track_pose_list[img_idx])
            else:
                cameras_list_track.append(self.track_camera_list[img_idx]) 
            img_measurements_track.append(img_Pt)
        if pose_estimates_track:
            pose_estimates.append(pose_estimates_track[0]) 
            pose_estimates.append(pose_estimates_track[-1])
        else:
            cameras_list = [cameras_list_track[0], cameras_list_track[-1]]
        img_measurements.append(img_measurements_track[0])
        img_measurements.append(img_measurements_track[-1])

        if len(pose_estimates) > 2 or len(cameras_list) > 2 or len(img_measurements) > 2:
            raise Exception("Nb of measurements should not be > 2. \
                Number of poses is: {}, number of cameras is: {} and number of observations is {}".format(
                    len(pose_estimates), 
                    len(cameras_list), 
                    len(img_measurements)))
        
        return pose_estimates, cameras_list, img_measurements


    def triangulate(self, track: gtsam.SfmTrack) -> gtsam.SfmTrack:
        """
        Args:
            track: feature track
        Returns:
            triangulated_landmark: triangulated landmark
        """

        pose_estimates, camera_values, img_measurements = self.extract_end_measurements(track)
        optimize = True
        rank_tol = 1e-9
        # if shared calibration provided for all cameras
        if self.sharedCal_Flag:
            if self.track_pose_list == None or not pose_estimates:
                raise Exception('track_poses arg or pose estimates missing')
            triangulated_pt = gtsam.triangulatePoint3(pose_estimates, self.calibration, img_measurements, rank_tol, optimize)
            track.setP(triangulated_pt)
            print("set pt sharedcal", track.point3(), triangulated_pt)
        else:
            if self.track_camera_list == None or not camera_values:
                raise Exception('track_cameras arg or camera values missing')
            triangulated_pt = gtsam.triangulatePoint3(camera_values, img_measurements, rank_tol, optimize)
            track.setP(triangulated_pt)
            print("set pt", track.point3(), triangulated_pt)
        return track
    
    def filter_reprojection_error(self, triangulated_pt: gtsam.Point3, track: gtsam.SfmTrack):
        # TODO: Add camera to input
        # TODO: Set threshold = 5*smallest_error in track?
        threshold = 5
        f_x = self.calibration[0][0]
        f_y = self.calibration[1][1]
        c_x = self.calibration[0][2]
        c_y = self.calibration[1][2]
        new_track = gtsam.SfmTrack()
        
        for measurement_idx in range(track.number_measurements()):
            pose_idx, measurement = track.measurement(measurement_idx)
            triangulated_pt = track.Point3()
            # Project to camera 1
            uc = f_x*camera.project(triangulated_pt) + c_x
            vc = f_y*camera.project(triangulated_pt) + c_y
            # Projection error in cam 1
            error = (uc - measurement[0])**2 + (vc - measurement[1])
            if error < threshold:
                new_track.add_measurement(measurement)
                new_track.setP(triangulated_pt)
        # TODO: return filtered track based on above discard mechanism
        return new_track

        
        