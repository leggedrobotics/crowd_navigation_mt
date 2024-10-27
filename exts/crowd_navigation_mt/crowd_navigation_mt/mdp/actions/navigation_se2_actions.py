# Copyright (c) 2022-2024, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import torch
from typing import TYPE_CHECKING

from omni.isaac.lab.envs import ManagerBasedRLEnv
from omni.isaac.lab.managers.action_manager import ActionTerm
from omni.isaac.lab.utils.assets import check_file_path, read_file

if TYPE_CHECKING:
    from .navigation_se2_actions_cfg import PerceptiveNavigationSE2ActionCfg


class PerceptiveNavigationSE2Action(ActionTerm):
    """Actions to navigate a robot by following some path."""

    cfg: PerceptiveNavigationSE2ActionCfg
    _env: ManagerBasedRLEnv

    def __init__(self, cfg: PerceptiveNavigationSE2ActionCfg, env: ManagerBasedRLEnv):
        super().__init__(cfg, env)
        # check if policy file exists
        if not check_file_path(cfg.low_level_policy_file):
            raise FileNotFoundError(f"Policy file '{cfg.low_level_policy_file}' does not exist.")
        # load policies
        file_bytes = read_file(self.cfg.low_level_policy_file)
        self.low_level_policy = torch.jit.load(file_bytes, map_location=self.device)
        self.low_level_policy = torch.jit.freeze(self.low_level_policy.eval())

        # prepare joint position actions
        self.low_level_action_term: ActionTerm = self.cfg.low_level_action.class_type(cfg.low_level_action, env)

        # prepare buffers
        self._action_dim = 3  # [vx, vy, omega]

        # set up buffers
        self._init_buffers()

        # for policies trained with Isaac Gym, reorder the joint based on a provided list of joint names
        self.joint_mapping_gym_to_sim = env.scene["robot"].find_joints(
            env.scene["robot"].joint_names, self.cfg.reorder_joint_list, preserve_order=True
        )[0]
        self.joint_mapping_sim_to_gym = [
            env.scene.articulations["robot"].joint_names.index(joint) for joint in self.cfg.reorder_joint_list
        ]

    """
    Properties.
    """

    @property
    def action_dim(self) -> int:
        return self._action_dim

    @property
    def raw_actions(self) -> torch.Tensor:
        return self._raw_navigation_velocity_actions

    @property
    def processed_actions(self) -> torch.Tensor:
        return self._processed_navigation_velocity_actions

    @property
    def low_level_actions(self) -> torch.Tensor:
        return self._low_level_actions

    @property
    def prev_low_level_actions(self) -> torch.Tensor:
        return self._prev_low_level_actions

    """
    Operations.
    """

    def process_actions(self, actions):
        """Process low-level navigation actions. This function is called with a frequency of 10Hz.

        Args:
            actions (torch.Tensor): The low-level navigation actions.
        """
        # Store the raw low-level navigation actions
        self._raw_navigation_velocity_actions[:] = actions
        # Apply the affine transformations
        if not self.cfg.use_raw_actions:
            self._processed_navigation_velocity_actions = (
                self._raw_navigation_velocity_actions * self._scale + self._offset
            )
            # self._processed_navigation_velocity_actions = torch.zeros(self.num_envs, self._action_dim, device=self.device)
        else:
            self._processed_navigation_velocity_actions[:] = self._raw_navigation_velocity_actions

        self._processed_navigation_velocity_actions = self._processed_navigation_velocity_actions * self._policy_scaling
        # elf._processed_navigation_velocity_actions = torch.tensor([0.5, 0.0, 0.0], device=self.device).repeat(self.num_envs, 1)

    def apply_actions(self):
        """Apply low-level actions for the simulator to the physics engine. This functions is called with the
        simulation frequency of 200Hz. Since low-level locomotion runs at 50Hz, we need to decimate the actions."""

        if self._counter % self.cfg.low_level_decimation == 0:
            self._counter = 0
            self._prev_low_level_actions[:] = self._low_level_actions.clone()
            # Get low level actions from low level policy
            actions_phase, self._hidden = self.low_level_policy(
                self._env.observation_manager.compute_group(group_name=self.cfg.observation_group), self._hidden
            )

            # process actions and bring them in the right order
            if not hasattr(self._env, "wild_anymal_obs"):
                self._low_level_actions[:] = self._env.scene.articulations["robot"].data.default_joint_pos
            else:
                self._low_level_actions[:] = self._env.wild_anymal_obs.store_action_and_get_joint_target(
                    actions_phase, use_raisim_order=True
                )

            if self.cfg.reorder_joint_list:
                self._low_level_actions = self._low_level_actions[:, self.joint_mapping_gym_to_sim]

            # Process low level actions
            self.low_level_action_term.process_actions(self._low_level_actions)

        # Apply low level actions
        self.low_level_action_term.apply_actions()
        self._counter += 1

        # substep update
        if not hasattr(self._env, "wild_anymal_obs"):
            return
        self._env.wild_anymal_obs.substep_update(
            self._env.scene.articulations["robot"].data.joint_pos[:, self.joint_mapping_sim_to_gym],
            self._env.scene.articulations["robot"].data.joint_vel[:, self.joint_mapping_sim_to_gym],
        )

    """
    Helper functions
    """

    def _init_buffers(self):
        # Prepare buffers
        self._raw_navigation_velocity_actions = torch.zeros(self.num_envs, self._action_dim, device=self.device)
        self._processed_navigation_velocity_actions = torch.zeros((self.num_envs, self._action_dim), device=self.device)
        self._low_level_actions = torch.zeros(self.num_envs, self.low_level_action_term.action_dim, device=self.device)
        self._prev_low_level_actions = torch.zeros_like(self._low_level_actions)
        self._low_level_step_dt = self.cfg.low_level_decimation * self._env.physics_dt
        self._counter = 0
        self._scale = torch.tensor(self.cfg.scale, device=self.device)
        self._offset = torch.tensor(self.cfg.offset, device=self.device)
        self._policy_scaling = torch.tensor(self.cfg.policy_scaling, device=self.device)
        self._hidden = torch.zeros(self.num_envs, 100, device=self.device)
