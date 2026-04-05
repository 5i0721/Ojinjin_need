import re
import asyncio
from typing import List, Tuple, Optional, Type

from src.common.logger import get_logger
from src.plugin_system import (
    BasePlugin,
    BaseCommand,
    register_plugin,
    ConfigField,
    ComponentInfo,
)

logger = get_logger("Ojinjin_need")


# ==================== 工具函数 ====================

def _parse_duration(raw: str) -> Optional[int]:
    """解析时长字符串，返回秒数"""
    if not raw:
        return None
    raw = raw.strip().lower()
    match = re.match(r"^(\d+)([smhd]?)$", raw)
    if not match:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    unit = match.group(2)
    multipliers: dict = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers.get(unit, 1)


def _format_duration(seconds: int) -> str:
    """将秒数格式化为可读时长"""
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}分{s}秒" if s else f"{m}分钟"
    elif seconds < 86400:
        h, remainder = divmod(seconds, 3600)
        m = remainder // 60
        return f"{h}小时{m}分钟" if m else f"{h}小时"
    else:
        d, remainder = divmod(seconds, 86400)
        h = remainder // 3600
        return f"{d}天{h}小时" if h else f"{d}天"


def _extract_target_qq(matched_groups: dict) -> str:
    """从匹配结果中提取目标QQ号，兼容两种@格式"""
    return (
        matched_groups.get("target_qq1")
        or matched_groups.get("target_qq2")
        or ""
    )


def _check_group_access(group_id: str, config_getter) -> bool:
    """
    检查群聊是否有权使用插件。
    
    黑白名单逻辑：
    - whitelist模式：列表为空=所有群生效，列表有值=只有列表中的群生效
    - blacklist模式：列表为空=所有群不生效，列表有值=列表中的群不生效、其余生效
    """
    mode: str = config_getter("group_filter.mode", "whitelist")
    group_list: list = config_getter("group_filter.group_list", [])
    group_str_list: list = [str(g) for g in group_list]

    if mode == "whitelist":
        # 白名单：空列表=全部通过；有列表=只有在列表中的通过
        if not group_str_list:
            return True
        return group_id in group_str_list
    else:
        # 黑名单：空列表=全部不通过；有列表=在列表中的不通过，其余通过
        if not group_str_list:
            return False
        return group_id not in group_str_list


def _check_user_access(user_id: str, config_getter) -> bool:
    """
    检查用户是否有权使用指令。
    
    黑白名单逻辑：
    - blacklist模式（默认）：列表为空=所有人可用；列表有值=列表中的人不可用
    - whitelist模式：列表为空=所有人不可用；列表有值=只有列表中的人可用
    """
    mode: str = config_getter("user_filter.mode", "blacklist")
    user_list: list = config_getter("user_filter.user_list", [])
    user_str_list: list = [str(u) for u in user_list]

    if mode == "blacklist":
        # 黑名单：空列表=全部通过；有列表=在列表中的不通过
        if not user_str_list:
            return True
        return user_id not in user_str_list
    else:
        # 白名单：空列表=全部不通过；有列表=只有在列表中的通过
        if not user_str_list:
            return False
        return user_id in user_str_list


# @格式的正则片段，兼容 @<昵称:QQ号> 和 [CQ:at,qq=QQ号]
_AT_PATTERN: str = r"(?:@<[^:]*:(?P<target_qq1>\d+)>|\[CQ:at,qq=(?P<target_qq2>\d+)\])"


# ==================== 禁言指令 ====================

class MuteCommand(BaseCommand):
    """禁言指令 - 「禁言 @用户 时长」，不加时长默认60秒"""

    command_name = "mute_user"
    command_description = "禁言指定用户一段时间"
    # 宽松匹配，时长可选
    command_pattern = rf"禁言\s*{_AT_PATTERN}\s*(?P<duration_raw>\d+[smhd]?)?"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        """执行禁言命令"""
        logger.info("========== 禁言指令触发 ==========")

        try:
            chat_stream = self.message.chat_stream
            message_info = self.message.message_info

            # 1. 群聊检查
            if not chat_stream or not chat_stream.group_info:
                logger.warning("禁言指令在非群聊环境中被触发")
                return False, "非群聊环境", True

            group_id: str = str(chat_stream.group_info.group_id)
            user_id: str = (
                str(message_info.user_info.user_id)
                if message_info and message_info.user_info
                else "unknown"
            )

            logger.info(f"触发者: {user_id}, 群: {group_id}")

            # 2. 群聊权限检查
            if not _check_group_access(group_id, self.get_config):
                logger.info(f"群 {group_id} 未通过群聊过滤，静默忽略")
                return False, "群未通过过滤", True

            # 3. 用户权限检查
            if not _check_user_access(user_id, self.get_config):
                logger.info(f"用户 {user_id} 未通过用户过滤，静默忽略")
                return False, "用户未通过过滤", True

            # 4. 提取参数
            target_qq: str = _extract_target_qq(self.matched_groups)
            duration_raw: str = self.matched_groups.get("duration_raw") or ""

            logger.info(f"解析参数 - 目标QQ: {target_qq}, 时长原始值: '{duration_raw}'")

            if not target_qq:
                logger.error(f"目标QQ解析失败")
                await self.send_text(
                    "指令格式错误，正确格式：禁言 @用户 时长\n"
                    "时长示例：60(秒)、10m(分钟)、1h(小时)、1d(天)\n"
                    "不填时长默认60秒"
                )
                return False, "参数解析失败", True

            # 5. 解析时长（不填默认60秒）
            if duration_raw:
                duration_seconds = _parse_duration(duration_raw)
                if duration_seconds is None:
                    logger.error(f"时长格式无法解析: {duration_raw}")
                    await self.send_text(
                        "时长格式错误，支持：纯数字(秒)、10m(分钟)、1h(小时)、1d(天)"
                    )
                    return False, "时长格式错误", True
            else:
                default_duration: int = self.get_config("mute.default_duration", 60)
                duration_seconds = default_duration
                logger.info(f"未指定时长，使用默认值: {duration_seconds}秒")

            # 6. 时长范围检查
            min_duration: int = self.get_config("mute.min_duration", 1)
            max_duration: int = self.get_config("mute.max_duration", 2592000)

            if duration_seconds < min_duration:
                logger.info(f"时长 {duration_seconds}s 低于最小值 {min_duration}s")
                await self.send_text(
                    f"禁言时长不能少于 {_format_duration(min_duration)}。"
                )
                return False, "时长过短", True

            if duration_seconds > max_duration:
                logger.info(f"时长 {duration_seconds}s 超过最大值 {max_duration}s")
                await self.send_text(
                    f"禁言时长不能超过 {_format_duration(max_duration)}。"
                )
                return False, "时长过长", True

            # 7. 发送禁言命令
            logger.info(
                f"发送禁言命令 - 目标: {target_qq}, 时长: {duration_seconds}s, 群: {group_id}"
            )

            success: bool = await self.send_command(
                command_name="GROUP_BAN",
                args={
                    "qq_id": str(target_qq),
                    "duration": str(duration_seconds),
                },
                display_message=f"禁言用户 {target_qq} {_format_duration(duration_seconds)}",
            )

            if success:
                duration_display: str = _format_duration(duration_seconds)
                logger.info(
                    f"✅ 禁言成功 - 群: {group_id}, 目标: {target_qq}, "
                    f"时长: {duration_display}, 操作者: {user_id}"
                )
                await self.send_text(f"已将用户 {target_qq} 禁言 {duration_display}。")
                return True, f"禁言 {target_qq} {duration_display}", True
            else:
                logger.error(f"❌ 禁言命令发送失败 - 群: {group_id}, 目标: {target_qq}")
                await self.send_text("禁言操作失败，请检查机器人是否有管理员权限。")
                return False, "禁言命令发送失败", True

        except Exception as e:
            logger.error(f"❌ 禁言指令执行异常: {e}", exc_info=True)
            await self.send_text("禁言操作出现错误，请查看日志。")
            return False, f"异常: {e}", True


# ==================== 鞭尸指令 ====================

class WhipCommand(BaseCommand):
    """鞭尸指令 - 反复禁言30天→解禁→禁言30天，默认10次"""

    command_name = "whip_user"
    command_description = "对用户执行鞭尸操作（反复禁言解禁）"
    # 宽松匹配，次数可选
    command_pattern = rf"鞭尸\s*{_AT_PATTERN}\s*(?P<count>\d+)?"

    # 30天的秒数
    WHIP_DURATION: int = 2592000

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        """执行鞭尸命令"""
        logger.info("========== 鞭尸指令触发 ==========")

        try:
            chat_stream = self.message.chat_stream
            message_info = self.message.message_info

            # 1. 群聊检查
            if not chat_stream or not chat_stream.group_info:
                logger.warning("鞭尸指令在非群聊环境中被触发")
                return False, "非群聊环境", True

            group_id: str = str(chat_stream.group_info.group_id)
            user_id: str = (
                str(message_info.user_info.user_id)
                if message_info and message_info.user_info
                else "unknown"
            )

            logger.info(f"触发者: {user_id}, 群: {group_id}")

            # 2. 群聊权限检查
            if not _check_group_access(group_id, self.get_config):
                logger.info(f"群 {group_id} 未通过群聊过滤，静默忽略")
                return False, "群未通过过滤", True

            # 3. 用户权限检查
            if not _check_user_access(user_id, self.get_config):
                logger.info(f"用户 {user_id} 未通过用户过滤，静默忽略")
                return False, "用户未通过过滤", True

            # 4. 提取参数
            target_qq: str = _extract_target_qq(self.matched_groups)
            count_raw: str = self.matched_groups.get("count") or ""

            if not target_qq:
                logger.error("鞭尸目标QQ解析失败")
                await self.send_text(
                    "指令格式错误，正确格式：鞭尸 @用户 [次数]\n"
                    "不填次数默认10次"
                )
                return False, "参数解析失败", True

            # 5. 解析次数
            default_count: int = self.get_config("whip.default_count", 10)
            max_count: int = self.get_config("whip.max_count", 100)

            if count_raw:
                try:
                    count = int(count_raw)
                    if count <= 0:
                        count = default_count
                except ValueError:
                    count = default_count
            else:
                count = default_count

            if count > max_count:
                logger.info(f"鞭尸次数 {count} 超过最大值 {max_count}，截断")
                await self.send_text(f"鞭尸次数不能超过 {max_count} 次。")
                return False, "次数过多", True

            logger.info(
                f"鞭尸参数 - 目标: {target_qq}, 次数: {count}, 群: {group_id}"
            )

            # 6. 执行鞭尸
            await self.send_text(
                f"开始对用户 {target_qq} 执行鞭尸，共 {count} 次..."
            )

            whip_delay: float = self.get_config("whip.interval_seconds", 1.0)
            success_count: int = 0
            fail_count: int = 0

            for i in range(count):
                # 禁言30天
                ban_success: bool = await self.send_command(
                    command_name="GROUP_BAN",
                    args={
                        "qq_id": str(target_qq),
                        "duration": str(self.WHIP_DURATION),
                    },
                    display_message=f"鞭尸第{i + 1}次-禁言",
                    storage_message=False,
                )

                if not ban_success:
                    logger.error(f"鞭尸第{i + 1}次禁言失败")
                    fail_count += 1
                    continue

                # 等待间隔
                await asyncio.sleep(whip_delay)

                # 解除禁言（duration=0）
                unban_success: bool = await self.send_command(
                    command_name="GROUP_BAN",
                    args={
                        "qq_id": str(target_qq),
                        "duration": "0",
                    },
                    display_message=f"鞭尸第{i + 1}次-解禁",
                    storage_message=False,
                )

                if not unban_success:
                    logger.error(f"鞭尸第{i + 1}次解禁失败")
                    fail_count += 1
                    continue

                success_count += 1

                # 等待间隔再进行下一轮
                if i < count - 1:
                    await asyncio.sleep(whip_delay)

            # 最后一次禁言30天（鞭完尸还是要关起来的）
            await self.send_command(
                command_name="GROUP_BAN",
                args={
                    "qq_id": str(target_qq),
                    "duration": str(self.WHIP_DURATION),
                },
                display_message=f"鞭尸完成-最终禁言",
                storage_message=False,
            )

            logger.info(
                f"✅ 鞭尸完成 - 目标: {target_qq}, "
                f"成功: {success_count}/{count}, 失败: {fail_count}"
            )

            await self.send_text(
                f"鞭尸完成！对用户 {target_qq} 执行了 {success_count}/{count} 次鞭尸，"
                f"最终禁言30天。"
            )

            return True, f"鞭尸 {target_qq} {count}次", True

        except Exception as e:
            logger.error(f"❌ 鞭尸指令执行异常: {e}", exc_info=True)
            await self.send_text("鞭尸操作出现错误，请查看日志。")
            return False, f"异常: {e}", True


# ==================== 插件主类 ====================

@register_plugin
class MuteCommandPlugin(BasePlugin):
    """禁言指令插件 - 支持禁言和鞭尸功能"""

    plugin_name: str = "Ojinjin_need"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本配置",
        "group_filter": "群聊过滤",
        "user_filter": "用户过滤",
        "mute": "禁言功能配置",
        "whip": "鞭尸功能配置",
    }

    config_schema = {
        "plugin": {
            "enabled": ConfigField(
                type=bool, default=True, description="是否启用插件"
            ),
            "config_version": ConfigField(
                type=str, default="2.0.1", description="配置文件版本"
            ),
        },
        "group_filter": {
            "mode": ConfigField(
                type=str,
                default="blacklist",
                description="群聊过滤模式：whitelist=只有列表中的群可用（列表为空则所有群可用）；blacklist=列表中的群禁用（列表为空则所有群禁用）",
                choices=["whitelist", "blacklist"],
            ),
            "group_list": ConfigField(
                type=list,
                default=[],
                description="群号列表。",
                item_type="string",
            ),
        },
        "user_filter": {
            "mode": ConfigField(
                type=str,
                default="whitelist",
                description="用户过滤模式：blacklist=列表中的用户禁用（列表为空则所有人可用）；whitelist=只有列表中的用户可用（列表为空则所有人禁用）",
                choices=["whitelist", "blacklist"],
            ),
            "user_list": ConfigField(
                type=list,
                default=[],
                description="用户QQ号列表。",
                item_type="string",
            ),
        },
        "mute": {
            "default_duration": ConfigField(
                type=int,
                default=60,
                description="不指定时长时的默认禁言时长（秒）",
            ),
            "min_duration": ConfigField(
                type=int, default=1, description="最短禁言时长（秒）"
            ),
            "max_duration": ConfigField(
                type=int, default=2592000, description="最长禁言时长（秒），默认30天"
            ),
        },
        "whip": {
            "default_count": ConfigField(
                type=int, default=5, description="鞭尸默认循环次数"
            ),
            "max_count": ConfigField(
                type=int, default=100, description="鞭尸最大循环次数"
            ),
            "interval_seconds": ConfigField(
                type=float,
                default=1.5,
                description="鞭尸每次操作之间的间隔（秒），太快可能被风控",
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        logger.info("✅ 注册 MuteCommand 和 WhipCommand 组件")
        return [
            (MuteCommand.get_command_info(), MuteCommand),
            (WhipCommand.get_command_info(), WhipCommand),
        ]