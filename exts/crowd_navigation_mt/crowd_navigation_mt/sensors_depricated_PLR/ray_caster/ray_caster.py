# Copyright (c) 2022-2024, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import numpy as np
import re
import torch
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

import carb
import omni.physics.tensors.impl.api as physx
import warp as wp
from omni.isaac.core.prims import XFormPrimView
from pxr import UsdPhysics

import omni.isaac.lab.sim as sim_utils
from omni.isaac.lab.markers import VisualizationMarkers
from omni.isaac.lab.utils import PRIMITIVE_MESH_TYPES, create_mesh_from_geom_shape, create_trimesh_from_geom_mesh
from omni.isaac.lab.utils.math import quat_apply, quat_apply_yaw
from omni.isaac.lab.utils.warp import convert_to_warp_mesh, raycast_dynamic_meshes

from ..sensor_base import SensorBase
from ..utils import compute_world_poses
from .ray_caster_data import RayCasterData

if TYPE_CHECKING:
    from .ray_caster_cfg import RayCasterCfg


class RayCaster(SensorBase):
    """A ray-casting sensor.

    The ray-caster uses a set of rays to detect collisions with meshes in the scene. The rays are
    defined in the sensor's local coordinate frame. The sensor can be configured to ray-cast against
    a set of meshes with a given ray pattern.

    The meshes are parsed from the list of primitive paths provided in the configuration. These are then
    converted to warp meshes and stored in the `warp_meshes` list. The ray-caster then ray-casts against
    these warp meshes using the ray pattern provided in the configuration.
    """

    cfg: RayCasterCfg
    """The configuration parameters."""
    meshes: ClassVar[dict[str, list[list[wp.Mesh]]]] = {}
    """The warp meshes available for raycasting. Stored as a dictionary.
        For each target_prim_cfg in the ray_caster_cfg.mesh_prim_paths, the dictionary stores the warp meshes
        for each environment instance. The list has shape (num_envs, num_meshes_per_env).
        Note that wp.Mesh are references to the warp mesh objects, so they are not duplicated for each environment if
        not necessary.

    The keys correspond to the prim path for the meshes, and values are the corresponding warp Mesh objects.

    Note:
           We store a global dictionary of all warp meshes to prevent re-loading the mesh for different ray-cast sensor instances.
    """
    mesh_views: ClassVar[dict[str, object]] = {}
    """The views of the meshes available for raycasting.

    The keys correspond to the prim path for the meshes, and values are the corresponding views of the prims.

    Note:
           We store a global dictionary of all views to prevent re-loading for different ray-cast sensor instances.
    """

    def __init__(self, cfg: RayCasterCfg):
        """Initializes the ray-caster object.

        Args:
            cfg: The configuration parameters.
        """
        # check if sensor path is valid
        # note: currently we do not handle environment indices if there is a regex pattern in the leaf
        #   For example, if the prim path is "/World/Sensor_[1,2]".
        sensor_path = cfg.prim_path.split("/")[-1]
        sensor_path_is_regex = re.match(r"^[a-zA-Z0-9/_]+$", sensor_path) is None
        if sensor_path_is_regex:
            raise RuntimeError(
                f"Invalid prim path for the ray-caster sensor: {self.cfg.prim_path}."
                "\n\tHint: Please ensure that the prim path does not contain any regex patterns in the leaf."
            )
        # Initialize base class
        super().__init__(cfg)
        # Create empty variables for storing output data
        self._data = RayCasterData()
        self._raycast_targets_cfg: list[RayCasterCfg.RaycastTargetCfg] = []

        self._num_meshes_per_env: dict[str, int] = {}
        """Keeps track of the number of meshes per env for each ray_cast target.
           Since we allow regex indexing (e.g. env_*/object_*) they can differ
        """

        for target in self.cfg.mesh_prim_paths:
            # Legacy support for string targets. Treat them as global targets.
            if isinstance(target, str):
                self._raycast_targets_cfg.append(cfg.RaycastTargetCfg(target_prim_expr=target, is_global=True))
            else:
                self._raycast_targets_cfg.append(target)

    def __str__(self) -> str:
        """Returns: A string containing information about the instance."""

        return (
            f"Ray-caster @ '{self.cfg.prim_path}': \n"
            f"\tview type            : {self._view.__class__}\n"
            f"\tupdate period (s)    : {self.cfg.update_period}\n"
            f"\tnumber of meshes     : {self._num_envs} x {sum(self._num_meshes_per_env.values())} \n"
            f"\tnumber of sensors    : {self._view.count}\n"
            f"\tnumber of rays/sensor: {self.num_rays}\n"
            f"\ttotal number of rays : {self.num_rays * self._view.count}"
        )

    """
    Properties
    """

    @property
    def num_instances(self) -> int:
        return self._view.count

    @property
    def data(self) -> RayCasterData:
        # update sensors if needed
        self._update_outdated_buffers()
        # return the data
        return self._data

    """
    Operations.
    """

    def reset(self, env_ids: Sequence[int] | None = None):
        # reset the timers and counters
        super().reset(env_ids)
        # resolve None
        if env_ids is None:
            env_ids = slice(None)
        # resample the drift
        self.drift = torch.zeros(self._view.count, 3, device=self.device)
        self.drift[env_ids].uniform_(*self.cfg.drift_range)

    """
    Implementation.
    """

    def _initialize_impl(self):
        super()._initialize_impl()
        # create simulation view
        self._physics_sim_view = physx.create_simulation_view(self._backend)
        self._physics_sim_view.set_subspace_roots("/")
        # check if the prim at path is an articulated or rigid prim
        # we do this since for physics-based view classes we can access their data directly
        # otherwise we need to use the xform view class which is slower
        found_supported_prim_class = False
        prim = sim_utils.find_first_matching_prim(self.cfg.prim_path)

        if prim is None:
            raise RuntimeError(f"Failed to find a prim at path expression: {self.cfg.prim_path}")
        # create view based on the type of prim
        if prim.HasAPI(UsdPhysics.ArticulationRootAPI):
            self._view = self._physics_sim_view.create_articulation_view(self.cfg.prim_path.replace(".*", "*"))
            found_supported_prim_class = True
        elif prim.HasAPI(UsdPhysics.RigidBodyAPI):
            self._view = self._physics_sim_view.create_rigid_body_view(self.cfg.prim_path.replace(".*", "*"))
            found_supported_prim_class = True
        else:
            self._view = XFormPrimView(self.cfg.prim_path, reset_xform_properties=False)
            found_supported_prim_class = True
            carb.log_warn(f"The prim at path {prim.GetPath().pathString} is not a physics prim! Using XFormPrimView.")
        # check if prim view class is found
        if not found_supported_prim_class:
            raise RuntimeError(f"Failed to find a valid prim view class for the prim paths: {self.cfg.prim_path}")

        # load the meshes by parsing the stage
        self._initialize_warp_meshes()
        # initialize the ray start and directions
        self._initialize_rays_impl()

    def _initialize_warp_meshes(self):
        for target_cfg in self._raycast_targets_cfg:
            # target prim path to ray cast against
            mesh_prim_path = target_cfg.target_prim_expr
            # check if mesh already casted into warp mesh and get the number of meshes per env
            if mesh_prim_path in RayCaster.meshes:
                self._num_meshes_per_env[mesh_prim_path] = len(RayCaster.meshes[mesh_prim_path]) // self._num_envs
                continue
            paths = sim_utils.find_matching_prim_paths(mesh_prim_path)
            if len(paths) == 0:
                raise RuntimeError(f"Failed to find a prim at path expression: {mesh_prim_path}")

            loaded_vertices: list[np.ndarray | None] = []
            wp_meshes = []
            for path in paths:
                # check if the prim is a primitive object - handle these as special types
                mesh_prim = sim_utils.get_first_matching_child_prim(
                    path, lambda prim: prim.GetTypeName() in PRIMITIVE_MESH_TYPES
                )

                # if we did not find a plane then we need to read the mesh
                if mesh_prim is None:

                    # obtain the mesh prim
                    mesh_prim = sim_utils.get_first_matching_child_prim(path, lambda prim: prim.GetTypeName() == "Mesh")

                    # check if valid
                    if mesh_prim is None or not mesh_prim.IsValid():
                        raise RuntimeError(f"Invalid mesh prim path: {paths}")

                    points, faces = create_trimesh_from_geom_mesh(mesh_prim)
                    points *= np.array(sim_utils.resolve_world_scale(mesh_prim))
                    registered_idx = _registered_points_idx(points, loaded_vertices)
                    if registered_idx != -1:
                        print("Found a duplicate mesh, only reference the mesh.")
                        # Found a duplicate mesh, only reference the mesh.
                        loaded_vertices.append(None)
                        wp_mesh = wp_meshes[registered_idx]
                    else:
                        loaded_vertices.append(points)
                        wp_mesh = convert_to_warp_mesh(points, faces, device=self.device)
                    # print info
                    carb.log_info(
                        f"Read mesh prim: {mesh_prim.GetPath()} with {len(points)} vertices and {len(faces)} faces."
                    )
                else:
                    # load the primitive mesh
                    mesh = create_mesh_from_geom_shape(mesh_prim)
                    mesh.vertices *= np.array(sim_utils.resolve_world_scale(mesh_prim))

                    registered_idx = _registered_points_idx(mesh.vertices, loaded_vertices)
                    if registered_idx != -1:
                        # Found a duplicate mesh, only reference the mesh.
                        loaded_vertices.append(None)
                        wp_mesh = wp_meshes[registered_idx]
                    else:
                        loaded_vertices.append(mesh.vertices)
                        wp_mesh = convert_to_warp_mesh(mesh.vertices, mesh.faces, device=self.device)
                    # print info
                    carb.log_info(f"Created {mesh_prim.GetTypeName()} mesh prim: {mesh_prim.GetPath()}.")
                wp_meshes.append(wp_mesh)

            if target_cfg.is_global:
                # reference the mesh for each environment to ray cast against
                RayCaster.meshes[mesh_prim_path] = [wp_meshes] * self._num_envs
                self._num_meshes_per_env[mesh_prim_path] = 1
            else:
                # split up the meshes for each environment. Little bit ugly, since
                # the current order is interleaved (env1_obj1, env1_obj2, env2_obj1, env2_obj2, ...)
                RayCaster.meshes[mesh_prim_path] = []
                mesh_idx = 0
                n_meshes_per_env = len(wp_meshes) // self._num_envs
                self._num_meshes_per_env[mesh_prim_path] = n_meshes_per_env
                for _ in range(self._num_envs):
                    RayCaster.meshes[mesh_prim_path].append(wp_meshes[mesh_idx : mesh_idx + n_meshes_per_env])
                    mesh_idx += n_meshes_per_env

            if self.cfg.track_mesh_transforms:
                # create view based on the type of prim
                mesh_prim_api = sim_utils.find_first_matching_prim(mesh_prim_path)
                if mesh_prim_api.HasAPI(UsdPhysics.ArticulationRootAPI):
                    RayCaster.mesh_views[mesh_prim_path] = self._physics_sim_view.create_articulation_view(
                        mesh_prim_path.replace(".*", "*")
                    )
                    carb.log_info(f"Created articulation view for mesh prim at path: {mesh_prim_path}")
                elif mesh_prim_api.HasAPI(UsdPhysics.RigidBodyAPI):
                    RayCaster.mesh_views[mesh_prim_path] = self._physics_sim_view.create_rigid_body_view(
                        mesh_prim_path.replace(".*", "*")
                    )
                    carb.log_info(f"Created rigid body view for mesh prim at path: {mesh_prim_path}")
                else:
                    RayCaster.mesh_views[mesh_prim_path] = XFormPrimView(mesh_prim_path, reset_xform_properties=False)
                    carb.log_warn(f"The prim at path {mesh_prim_path} is not a physics prim! Using XFormPrimView.")

        # throw an error if no meshes are found
        if all([target_cfg.target_prim_expr not in RayCaster.meshes for target_cfg in self._raycast_targets_cfg]):
            raise RuntimeError(
                f"No meshes found for ray-casting! Please check the mesh prim paths: {self.cfg.mesh_prim_paths}"
            )
        
        """code fragments not sure if working"""
        """# TODO check if this works
        total_n_meshes_per_env = 0
        for curr_path in self.cfg.mesh_prim_paths:
            if isinstance(curr_path, str):
                total_n_meshes_per_env += self._num_envs if "*" in curr_path else 1
            else:
                total_n_meshes_per_env += self._num_envs if "*" in curr_path.target_prim_expr else 1
        """

        if self.cfg.track_mesh_transforms:
            total_n_meshes_per_env = sum(self._num_meshes_per_env.values())
            self._mesh_positions_w = torch.zeros(self._num_envs, total_n_meshes_per_env, 3, device=self.device)
            self._mesh_orientations_w = torch.zeros(self._num_envs, total_n_meshes_per_env, 4, device=self.device)
            self._data.mesh_velocities_w = torch.zeros(self._num_envs, total_n_meshes_per_env, 3, device=self.device)
            self._data.mesh_angular_velocities_w = torch.zeros(
                self._num_envs, total_n_meshes_per_env, 3, device=self.device
            )
        # flatten the list of meshes that are included in mesh_prim_paths of the specific ray caster
        self._meshes = []
        for env_idx in range(self._num_envs):
            meshes_in_env = []
            for target_cfg in self._raycast_targets_cfg:
                meshes_in_env.extend(RayCaster.meshes[target_cfg.target_prim_expr][env_idx])
            self._meshes.append(meshes_in_env)

        if self.cfg.track_mesh_transforms:
            self._mesh_views = [
                RayCaster.mesh_views[target_cfg.target_prim_expr] for target_cfg in self._raycast_targets_cfg
            ]

        # save a warp array with mesh ids that is passed to the raycast function
        self._mesh_ids_wp = wp.array2d([[m.id for m in b] for b in self._meshes], dtype=wp.uint64, device=self.device)

    def _initialize_rays_impl(self):
        # compute ray stars and directions
        self.ray_starts, self.ray_directions = self.cfg.pattern_cfg.func(self.cfg.pattern_cfg, self._device)
        self.num_rays = len(self.ray_directions)
        # apply offset transformation to the rays
        offset_pos = torch.tensor(list(self.cfg.offset.pos), device=self._device)
        offset_quat = torch.tensor(list(self.cfg.offset.rot), device=self._device)
        self.ray_directions = quat_apply(offset_quat.repeat(len(self.ray_directions), 1), self.ray_directions)
        self.ray_starts += offset_pos
        # repeat the rays for each sensor
        self.ray_starts = self.ray_starts.repeat(self._view.count, 1, 1)
        self.ray_directions = self.ray_directions.repeat(self._view.count, 1, 1)
        # prepare drift
        self.drift = torch.zeros(self._view.count, 3, device=self.device)
        # fill the data buffer
        self._data.pos_w = torch.zeros(self._view.count, 3, device=self.device)
        self._data.quat_w = torch.zeros(self._view.count, 4, device=self.device)
        self._data.ray_hits_w = torch.zeros(self._view.count, self.num_rays, 3, device=self.device)
        
        """crowdnavigation implementation for teacher policy"""
        self._data.vel_w = torch.zeros(self._view.count, 3, device=self.device)
        self._data.rot_vel_w = torch.zeros(self._view.count, 3, device=self.device)
        self._data.ray_hit_mesh_idx = torch.zeros(
            self._view.count, self.num_rays, 1, device=self.device, dtype=torch.int64
        )
        self._data.distances = torch.zeros(self._view.count, self.num_rays, device=self.device)
        #self.data.distances = torch.zeros((env.num_envs, self.history_length, sensor.data.pos_w.shape[-1])).to(env.device)
    def _update_buffers_impl(self, env_ids: Sequence[int]):
        """Fills the buffers of the sensor data."""
        # obtain the poses of the sensors
        pos_w, quat_w = compute_world_poses(self._view, env_ids)
        # note: we clone here because we are read-only operations
        pos_w = pos_w.clone()
        quat_w = quat_w.clone()
        # apply drift
        pos_w += self.drift[env_ids]
        # store the poses
        self._data.pos_w[env_ids] = pos_w
        self._data.quat_w[env_ids] = quat_w

        # store the velocities
        vel_w, omega_w = _get_world_velocities(self._view, env_ids)
        #self._data.vel_w = torch.zeros(self._view.count, 3, device=self.device)
        #self._data.rot_vel_w = torch.zeros(self._view.count, 3, device=self.device)
        self._data.vel_w[env_ids] = vel_w
        self._data.rot_vel_w[env_ids] = omega_w

        # ray cast based on the sensor poses
        if self.cfg.attach_yaw_only:
            # only yaw orientation is considered and directions are not rotated
            ray_starts_w = quat_apply_yaw(quat_w.repeat(1, self.num_rays), self.ray_starts[env_ids])
            ray_starts_w += pos_w.unsqueeze(1)
            # ray_directions_w = self.ray_directions[env_ids]  # why not rotated?
            ray_directions_w = quat_apply_yaw(quat_w.repeat(1, self.num_rays), self.ray_directions[env_ids])

        else:
            # full orientation is considered
            ray_starts_w = quat_apply(quat_w.repeat(1, self.num_rays), self.ray_starts[env_ids])
            ray_starts_w += pos_w.unsqueeze(1)
            ray_directions_w = quat_apply(quat_w.repeat(1, self.num_rays), self.ray_directions[env_ids])

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

        self._data.ray_hits_w[env_ids] = raycast_dynamic_meshes(
            ray_starts_w,
            ray_directions_w,
            mesh_ids_wp=self._mesh_ids_wp,  # list with shape num_envs x num_meshes_per_env
            max_dist=self.cfg.max_distance,
            mesh_positions_w=self._mesh_positions_w[env_ids] if self.cfg.track_mesh_transforms else None,
            mesh_orientations_w=self._mesh_orientations_w[env_ids] if self.cfg.track_mesh_transforms else None,
        )[0]

    def _set_debug_vis_impl(self, debug_vis: bool):
        # set visibility of markers
        # note: parent only deals with callbacks. not their visibility
        if debug_vis:
            if not hasattr(self, "ray_visualizer"):
                self.ray_visualizer = VisualizationMarkers(self.cfg.visualizer_cfg)
            # set their visibility to true
            self.ray_visualizer.set_visibility(True)
        else:
            if hasattr(self, "ray_visualizer"):
                self.ray_visualizer.set_visibility(False)

    def _debug_vis_callback(self, event):
        # show ray hit positions
        # only show the first sensor and segment based on floor hit vs obstacle hit
        # self.ray_visualizer.visualize(
        #     self._data.ray_hits_w[0], marker_indices=self._data.ray_hit_mesh_idx[0].view(-1) != 0
        # )
        self.ray_visualizer.visualize(self._data.ray_hits_w.view(-1, 3))

    """
    Internal simulation callbacks.
    """

    def _invalidate_initialize_callback(self, event):
        """Invalidates the scene elements."""
        # call parent
        super()._invalidate_initialize_callback(event)
        # set all existing views to None to invalidate them
        self._physics_sim_view = None
        self._view = None


"""
Helper functions
"""


def _registered_points_idx(points: np.ndarray, registered_points: list[np.ndarray | None]) -> int:
    """Check if the points are already registered in the list of registered points.

    Args:
        points: The points to check.
        registered_points: The list of registered points.

    Returns:
        The index of the registered points if found, otherwise -1.
    """
    for idx, reg_points in enumerate(registered_points):
        if reg_points is None:
            continue
        if reg_points.shape == points.shape and (reg_points == points).all():
            return idx
    return -1


def _get_world_velocities(
    physxView: XFormPrimView | physx.ArticulationView | physx.RigidBodyView,
    env_ids: torch.Tensor | None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Get the velocities of the prim referenced by the prim view.

    Args:
        physxView: The prim view to get the world poses from.
        env_ids: The environment ids of the prims to get the world poses for.

    Raises:
        ValueError: If the prim view is not of the correct type.

    Returns:
        A tuple containing the world linear and angular velocities of the prims.
    """
    if isinstance(physxView, XFormPrimView):
        v_w = omega_w = torch.zeros_like(physxView.get_world_poses(env_ids)[0])
    elif isinstance(physxView, physx.ArticulationView):
        v_w, omega_w = physxView.get_root_velocities()[env_ids].split([3, 3], dim=-1)
    elif isinstance(physxView, physx.RigidBodyView):
        v_w, omega_w = physxView.get_velocities()[env_ids].split([3, 3], dim=-1)
    else:
        raise ValueError(f"Cannot get world poses for prim view of type '{type(physxView)}'.")

    return v_w, omega_w