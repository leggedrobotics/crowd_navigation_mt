# Copyright (c) 2022-2024, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import numpy as np
import torch
from collections.abc import Sequence
from tensordict import TensorDict
from typing import TYPE_CHECKING, ClassVar, Literal

import omni.physics.tensors.impl.api as physx
import warp as wp
from omni.isaac.core.prims import XFormPrimView

import omni.isaac.lab.utils.math as math_utils
from omni.isaac.lab.sensors.camera import CameraData
from omni.isaac.lab.sensors.camera.utils import convert_orientation_convention, create_rotation_matrix_from_view
from omni.isaac.lab.utils.math import convert_quat
from omni.isaac.lab.utils.warp import raycast_dynamic_meshes

from ..utils import compute_world_poses
from .ray_caster import RayCaster

if TYPE_CHECKING:
    from .ray_caster_camera_cfg import RayCasterCameraCfg


class RayCasterCamera(RayCaster):
    """A ray-casting camera sensor.

    The ray-caster camera uses a set of rays to get the distances to meshes in the scene. The rays are
    defined in the sensor's local coordinate frame. The sensor has the same interface as the
    :class:`omni.isaac.ISAACLAB.sensors.Camera` that implements the camera class through USD camera prims.
    However, this class provides a faster image generation. The sensor converts meshes from the list of
    primitive paths provided in the configuration to Warp meshes. The camera then ray-casts against these
    Warp meshes only.

    Currently, only the following annotators are supported:

    - ``"distance_to_camera"``: An image containing the distance to camera optical center.
    - ``"distance_to_image_plane"``: An image containing distances of 3D points from camera plane along camera's z-axis.
    - ``"normals"``: An image containing the local surface normal vectors at each pixel.
    """

    cfg: RayCasterCameraCfg
    """The configuration parameters."""
    UNSUPPORTED_TYPES: ClassVar[set[str]] = {
        "rgb",
        "instance_id_segmentation",
        "instance_id_segmentation_fast",
        "instance_segmentation",
        "instance_segmentation_fast",
        "semantic_segmentation",
        "skeleton_data",
        "motion_vectors",
        "bounding_box_2d_tight",
        "bounding_box_2d_tight_fast",
        "bounding_box_2d_loose",
        "bounding_box_2d_loose_fast",
        "bounding_box_3d",
        "bounding_box_3d_fast",
    }
    """A set of sensor types that are not supported by the ray-caster camera."""

    def __init__(self, cfg: RayCasterCameraCfg):
        """Initializes the camera object.

        Args:
            cfg: The configuration parameters.

        Raises:
            ValueError: If the provided data types are not supported by the ray-caster camera.
        """
        # perform check on supported data types
        self._check_supported_data_types(cfg)
        # initialize base class
        super().__init__(cfg)
        # create empty variables for storing output data
        self._cam_cam_data = CameraData()

    def __str__(self) -> str:
        """Returns: A string containing information about the instance."""
        return (
            f"Ray-Caster-Camera @ '{self.cfg.prim_path}': \n"
            f"\tview type            : {self._view.__class__}\n"
            f"\tupdate period (s)    : {self.cfg.update_period}\n"
            f"\tnumber of meshes     : {len(RayCaster.meshes)}\n"
            f"\tnumber of sensors    : {self._view.count}\n"
            f"\tnumber of rays/sensor: {self.num_rays}\n"
            f"\ttotal number of rays : {self.num_rays * self._view.count}\n"
            f"\timage shape          : {self.image_shape}"
        )

    """
    Properties
    """

    @property
    def camera_data(self) -> CameraData:
        # update sensors if needed
        self._update_outdated_buffers()
        # return the data
        return self._cam_data

    @property
    def image_shape(self) -> tuple[int, int]:
        """A tuple containing (height, width) of the camera sensor."""
        return (self.cfg.pattern_cfg.height, self.cfg.pattern_cfg.width)

    @property
    def frame(self) -> torch.tensor:
        """Frame number when the measurement took place."""
        return self._frame

    """
    Operations.
    """

    def set_intrinsic_matrices(
        self, matrices: torch.Tensor, focal_length: float = 1.0, env_ids: Sequence[int] | None = None
    ):
        """Set the intrinsic matrix of the camera.

        Args:
            matrices: The intrinsic matrices for the camera. Shape is (N, 3, 3).
            focal_length: Focal length to use when computing aperture values. Defaults to 1.0.
            env_ids: A sensor ids to manipulate. Defaults to None, which means all sensor indices.
        """
        # resolve env_ids
        if env_ids is None:
            env_ids = slice(None)
        # save new intrinsic matrices and focal length
        self._cam_data.intrinsic_matrices[env_ids] = matrices.to(self._device)
        self._focal_length = focal_length
        # recompute ray directions
        self.ray_starts[env_ids], self.ray_directions[env_ids] = self.cfg.pattern_cfg.func(
            self.cfg.pattern_cfg, self._cam_data.intrinsic_matrices[env_ids], self._device
        )

    def reset(self, env_ids: Sequence[int] | None = None):
        # reset the timestamps
        super().reset(env_ids)
        # resolve None
        if env_ids is None:
            env_ids = slice(None)
        # reset the data
        # note: this recomputation is useful if one performs events such as randomizations on the camera poses.
        pos_w, quat_w = self._compute_camera_world_poses(env_ids)
        self._cam_data.pos_w[env_ids] = pos_w
        self._cam_data.quat_w_world[env_ids] = quat_w
        # Reset the frame count
        self._frame[env_ids] = 0

    def set_world_poses(
        self,
        positions: torch.Tensor | None = None,
        orientations: torch.Tensor | None = None,
        env_ids: Sequence[int] | None = None,
        convention: Literal["opengl", "ros", "world"] = "ros",
    ):
        """Set the pose of the camera w.r.t. the world frame using specified convention.

        Since different fields use different conventions for camera orientations, the method allows users to
        set the camera poses in the specified convention. Possible conventions are:

        - :obj:`"opengl"` - forward axis: -Z - up axis +Y - Offset is applied in the OpenGL (Usd.Camera) convention
        - :obj:`"ros"`    - forward axis: +Z - up axis -Y - Offset is applied in the ROS convention
        - :obj:`"world"`  - forward axis: +X - up axis +Z - Offset is applied in the World Frame convention

        See :meth:`omni.isaac.lab.sensors.camera.utils.convert_orientation_convention` for more details
        on the conventions.

        Args:
            positions: The cartesian coordinates (in meters). Shape is (N, 3).
                Defaults to None, in which case the camera position in not changed.
            orientations: The quaternion orientation in (w, x, y, z). Shape is (N, 4).
                Defaults to None, in which case the camera orientation in not changed.
            env_ids: A sensor ids to manipulate. Defaults to None, which means all sensor indices.
            convention: The convention in which the poses are fed. Defaults to "ros".

        Raises:
            RuntimeError: If the camera prim is not set. Need to call :meth:`initialize` method first.
        """
        # resolve env_ids
        if env_ids is None:
            env_ids = self._ALL_INDICES

        # get current positions
        pos_w, quat_w = compute_world_poses(self._view, env_ids)
        if positions is not None:
            # transform to camera frame
            pos_offset_world_frame = positions - pos_w
            self._offset_pos[env_ids] = math_utils.quat_apply(math_utils.quat_inv(quat_w), pos_offset_world_frame)
        if orientations is not None:
            # convert rotation matrix from input convention to world
            quat_w_set = convert_orientation_convention(orientations, origin=convention, target="world")
            self._offset_quat[env_ids] = math_utils.quat_mul(math_utils.quat_inv(quat_w), quat_w_set)

        # update the data
        pos_w, quat_w = self._compute_camera_world_poses(env_ids)
        self._cam_data.pos_w[env_ids] = pos_w
        self._cam_data.quat_w_world[env_ids] = quat_w

    def set_world_poses_from_view(
        self, eyes: torch.Tensor, targets: torch.Tensor, env_ids: Sequence[int] | None = None
    ):
        """Set the poses of the camera from the eye position and look-at target position.

        Args:
            eyes: The positions of the camera's eye. Shape is N, 3).
            targets: The target locations to look at. Shape is (N, 3).
            env_ids: A sensor ids to manipulate. Defaults to None, which means all sensor indices.

        Raises:
            RuntimeError: If the camera prim is not set. Need to call :meth:`initialize` method first.
            NotImplementedError: If the stage up-axis is not "Y" or "Z".
        """
        # camera position and rotation in opengl convention
        orientations = math_utils.quat_from_matrix(create_rotation_matrix_from_view(eyes, targets, device=self._device))
        self.set_world_poses(eyes, orientations, env_ids, convention="opengl")

    """
    Implementation.
    """

    def _initialize_rays_impl(self):
        # Create all indices buffer
        self._ALL_INDICES = torch.arange(self._view.count, device=self._device, dtype=torch.long)
        # Create frame count buffer
        self._frame = torch.zeros(self._view.count, device=self._device, dtype=torch.long)
        # create buffers
        self._create_buffers()
        # compute intrinsic matrices
        self._compute_intrinsic_matrices()
        # compute ray stars and directions
        self.ray_starts, self.ray_directions = self.cfg.pattern_cfg.func(
            self.cfg.pattern_cfg, self._cam_data.intrinsic_matrices, self._device
        )
        self.num_rays = self.ray_directions.shape[1]
        # create buffer to store ray hits
        self.ray_hits_w = torch.zeros(self._view.count, self.num_rays, 3, device=self._device)
        # set offsets
        quat_w = convert_orientation_convention(
            torch.tensor([self.cfg.offset.rot], device=self._device), origin=self.cfg.offset.convention, target="world"
        )
        self._offset_quat = quat_w.repeat(self._view.count, 1)
        self._offset_pos = torch.tensor(list(self.cfg.offset.pos), device=self._device).repeat(self._view.count, 1)

    def _update_buffers_impl(self, env_ids: Sequence[int]):
        """Fills the buffers of the sensor data."""
        # increment frame count
        self._frame[env_ids] += 1

        # compute poses from current view
        pos_w, quat_w = self._compute_camera_world_poses(env_ids)
        # update the data
        self._cam_data.pos_w[env_ids] = pos_w
        self._cam_data.quat_w_world[env_ids] = quat_w

        # note: full orientation is considered
        ray_starts_w = math_utils.quat_apply(quat_w.repeat(1, self.num_rays), self.ray_starts[env_ids])
        ray_starts_w += pos_w.unsqueeze(1)
        ray_directions_w = math_utils.quat_apply(quat_w.repeat(1, self.num_rays), self.ray_directions[env_ids])

        if self.cfg.track_mesh_transforms:
            # Update the mesh positions and rotations
            mesh_idx = 0
            for view, target_cfg in zip(self._mesh_views, self._raycast_targets_cfg):
                # update position of the target meshes
                pos_w, ori_w = compute_world_poses(view, None)
                pos_w = pos_w.squeeze(0) if len(pos_w.shape) == 3 else pos_w
                ori_w = ori_w.squeeze(0) if len(ori_w.shape) == 3 else ori_w

                count = view.count
                if not target_cfg.is_global:
                    count = count // self._num_envs
                    pos_w = pos_w.view(self._num_envs, count, 3)
                    ori_w = ori_w.view(self._num_envs, count, 4)

                self._mesh_positions_w[:, mesh_idx : mesh_idx + count] = pos_w
                self._mesh_orientations_w[:, mesh_idx : mesh_idx + count] = ori_w
                mesh_idx += count

        # ray cast and store the hits
        self.ray_hits_w, ray_depth, ray_normal = raycast_dynamic_meshes(
            ray_starts_w,
            ray_directions_w,
            mesh_ids_wp=self._mesh_ids_wp,  # list with shape num_envs x num_meshes_per_env
            max_dist=self.cfg.max_distance,
            mesh_positions_w=self._mesh_positions_w[env_ids] if self.cfg.track_mesh_transforms else None,
            mesh_orientations_w=self._mesh_orientations_w[env_ids] if self.cfg.track_mesh_transforms else None,
            return_distance=any(
                [name in self.cfg.data_types for name in ["distance_to_image_plane", "distance_to_camera"]]
            ),
            return_normal="normals" in self.cfg.data_types,
        )[:3]
        # update output buffers
        if "distance_to_image_plane" in self.cfg.data_types:
            # note: data is in camera frame so we only take the first component (z-axis of camera frame)
            distance_to_image_plane = (
                math_utils.quat_apply(
                    math_utils.quat_inv(quat_w).repeat(1, self.num_rays),
                    (ray_depth[:, :, None] * ray_directions_w),
                )
            )[:, :, 0]
            self._cam_data.output["distance_to_image_plane"][env_ids] = distance_to_image_plane.view(
                -1, *self.image_shape
            )
        if "distance_to_camera" in self.cfg.data_types:
            self._cam_data.output["distance_to_camera"][env_ids] = ray_depth.view(-1, *self.image_shape)
        if "normals" in self.cfg.data_types:
            self._cam_data.output["normals"][env_ids] = ray_normal.view(-1, *self.image_shape, 3)

    def _debug_vis_callback(self, event):
        # in case it crashes be safe
        if not hasattr(self, "ray_hits_w"):
            return
        # show ray hit positions
        self.ray_visualizer.visualize(self.ray_hits_w.view(-1, 3))

    """
    Private Helpers
    """

    def _check_supported_data_types(self, cfg: RayCasterCameraCfg):
        """Checks if the data types are supported by the ray-caster camera."""
        # check if there is any intersection in unsupported types
        # reason: we cannot obtain this data from simplified warp-based ray caster
        common_elements = set(cfg.data_types) & RayCasterCamera.UNSUPPORTED_TYPES
        if common_elements:
            raise ValueError(
                f"RayCasterCamera class does not support the following sensor types: {common_elements}."
                "\n\tThis is because these sensor types cannot be obtained in a fast way using ''warp''."
                "\n\tHint: If you need to work with these sensor types, we recommend using the USD camera"
                " interface from the omni.isaac.lab.sensors.camera module."
            )

    def _create_buffers(self):
        """Create buffers for storing data."""
        # prepare drift
        self.drift = torch.zeros(self._view.count, 3, device=self.device)
        # create the data object
        # -- pose of the cameras
        self._cam_data.pos_w = torch.zeros((self._view.count, 3), device=self._device)
        self._cam_data.quat_w_world = torch.zeros((self._view.count, 4), device=self._device)
        # -- intrinsic matrix
        self._cam_data.intrinsic_matrices = torch.zeros((self._view.count, 3, 3), device=self._device)
        self._cam_data.intrinsic_matrices[:, 2, 2] = 1.0
        self._cam_data.image_shape = self.image_shape
        # -- output data
        # create the buffers to store the annotator data.
        self._cam_data.output = TensorDict({}, batch_size=self._view.count, device=self.device)
        self._cam_data.info = [{name: None for name in self.cfg.data_types}] * self._view.count
        for name in self.cfg.data_types:
            if name in ["distance_to_image_plane", "distance_to_camera"]:
                shape = (self.cfg.pattern_cfg.height, self.cfg.pattern_cfg.width)
            elif name in ["normals"]:
                shape = (self.cfg.pattern_cfg.height, self.cfg.pattern_cfg.width, 3)
            else:
                raise ValueError(f"Received unknown data type: {name}. Please check the configuration.")
            # allocate tensor to store the data
            self._cam_data.output[name] = torch.zeros((self._view.count, *shape), device=self._device)

    def _compute_intrinsic_matrices(self):
        """Computes the intrinsic matrices for the camera based on the config provided."""
        # compute the intrinsic matrix
        vertical_aperture = (
            self.cfg.pattern_cfg.horizontal_aperture * self.cfg.pattern_cfg.height / self.cfg.pattern_cfg.width
        )
        f_x = self.cfg.pattern_cfg.width * self.cfg.pattern_cfg.focal_length / self.cfg.pattern_cfg.horizontal_aperture
        f_y = self.cfg.pattern_cfg.height * self.cfg.pattern_cfg.focal_length / vertical_aperture
        c_x = self.cfg.pattern_cfg.horizontal_aperture_offset * f_x + self.cfg.pattern_cfg.width / 2
        c_y = self.cfg.pattern_cfg.vertical_aperture_offset * f_y + self.cfg.pattern_cfg.height / 2
        # allocate the intrinsic matrices
        self._cam_data.intrinsic_matrices[:, 0, 0] = f_x
        self._cam_data.intrinsic_matrices[:, 0, 2] = c_x
        self._cam_data.intrinsic_matrices[:, 1, 1] = f_y
        self._cam_data.intrinsic_matrices[:, 1, 2] = c_y
        # save focal length
        self._focal_length = self.cfg.pattern_cfg.focal_length

    def _compute_camera_world_poses(self, env_ids: Sequence[int]) -> tuple[torch.Tensor, torch.Tensor]:
        """Computes the pose of the camera in the world frame.

        This function applies the offset pose to the pose of the view the camera is attached to.

        Returns:
            A tuple of the position (in meters) and quaternion (w, x, y, z) in "world" convention.
        """
        # get the pose of the view the camera is attached to
        pos_w, quat_w = compute_world_poses(self._view, env_ids)
        # apply offsets
        # need to apply quat because offset relative to parent frame
        pos_w += math_utils.quat_apply(quat_w, self._offset_pos[env_ids])
        quat_w = math_utils.quat_mul(quat_w, self._offset_quat[env_ids])

        return pos_w, quat_w


"""
Helper functions
"""


def _get_world_poses(
    physxView: XFormPrimView | physx.ArticulationView | physx.RigidBodyView,
    env_ids: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Get the world poses of the prim referenced by the prim view.

    Args:
        physxView: The prim view to get the world poses from.
        env_ids: The environment ids of the prims to get the world poses for.

    Raises:
        ValueError: If the prim view is not of the correct type.

    Returns:
        A tuple containing the world positions and orientations of the prims. Orientation is in wxyz format.
    """
    if isinstance(physxView, XFormPrimView):
        pos_w, quat_w = physxView.get_world_poses(env_ids)
    elif isinstance(physxView, physx.ArticulationView):
        pos_w, quat_w = physxView.get_root_transforms()[env_ids].split([3, 4], dim=-1)
        quat_w = convert_quat(quat_w, to="wxyz")
    elif isinstance(physxView, physx.RigidBodyView):
        pos_w, quat_w = physxView.get_transforms()[env_ids].split([3, 4], dim=-1)
        quat_w = convert_quat(quat_w, to="wxyz")
    else:
        raise ValueError(f"Cannot get world poses for prim view of type '{type(physxView)}'.")

    return pos_w, quat_w
