"""权限控制：黑白名单与管理员判定.

规则优先级（高 -> 低）：
1. 屏蔽 QQ（blocked_users）：私聊/群聊一律拒绝；
2. 群聊黑名单 group_blacklist -> 群聊白名单 group_whitelist（留空 = 不限制）；
3. 私聊黑名单 private_blacklist -> 私聊白名单 private_whitelist（留空 = 不限制）。

被拒绝的消息不转发也不拦截，交由 MaiBot 原有流程处理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .plugin import GscoreAdapterPlugin


def _norm_ids(values: list[str] | None) -> set[str]:
    """把配置里的 QQ 号/群号列表归一化为去空白字符串集合。"""
    return {str(v).strip() for v in (values or []) if str(v).strip()}


def is_admin(plugin: "GscoreAdapterPlugin", user_id: str) -> bool:
    """是否管理员；未配置管理员列表时不限制（保持开箱即用）。"""
    admins = _norm_ids(plugin.config.permission.admin_users)
    if not admins:
        return True
    return user_id in admins


def user_pm_for(plugin: "GscoreAdapterPlugin", user_id: str) -> int:
    """计算上报 core 的用户权限等级。

    gsuid_core 中数值越小权限越高（6 普通 / 3 管理 / 2 群主）：
    管理员至少授予 pm=3，其余用户使用配置值。
    """
    gs_pm = max(1, int(plugin.config.gscore.user_pm))
    if user_id in _norm_ids(plugin.config.permission.admin_users):
        return min(gs_pm, 3)
    return gs_pm


def check_forward(
    plugin: "GscoreAdapterPlugin", *, user_id: str, group_id: str | None
) -> tuple[bool, str]:
    """判定一条消息是否允许转发，返回 (是否允许, 拒绝原因)。"""
    perm = plugin.config.permission
    if user_id in _norm_ids(perm.blocked_users):
        return False, f"用户 {user_id} 已被屏蔽"
    if group_id is not None:
        if group_id in _norm_ids(perm.group_blacklist):
            return False, f"群 {group_id} 在群聊黑名单中"
        whitelist = _norm_ids(perm.group_whitelist)
        if whitelist and group_id not in whitelist:
            return False, f"群 {group_id} 不在群聊白名单中"
    else:
        if user_id in _norm_ids(perm.private_blacklist):
            return False, f"用户 {user_id} 在私聊黑名单中"
        whitelist = _norm_ids(perm.private_whitelist)
        if whitelist and user_id not in whitelist:
            return False, f"用户 {user_id} 不在私聊白名单中"
    return True, ""
