import asyncio
import json
import websockets
import re
from typing import Dict, Set, Optional, List
import logging
from datetime import datetime, timedelta
from functools import wraps

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 配置部分
WS_URL = "ws://154.64.254.98:9181"
ACCESS_TOKEN = "196183"
ADMIN_GROUP_ID = 923820685
SLEEP_TARGET_ID = 1724270068  # 战云用户ID

# 启用的群组列表（只有在这些群中才会启用bot）
ENABLED_GROUPS = {
    923820685,  # 管理群
    123456789,  # 示例群组1
    987654321   # 示例群组2
}

# 违禁词库（支持正则表达式）
LEVEL_3_WORDS = {r"kukemc", r"kuke", r"酷可", r"kamu", r"咖目"}  # 直接踢出
LEVEL_2_WORDS = {r"以色列", r"女大", r"特朗普"}                   # 禁言1天 
LEVEL_1_WORDS = {r"傻[逼屄]", r"脑残", r"死妈"}                # 禁言10分钟

# 广告检测规则 - 优化版本
AD_PATTERNS = {
    # 排除CQ码中的内容，避免匹配表情/图片中的参数
    r"(?:\d{8,})(?![^\[]*\])",        # 长数字（排除CQ码中的）
    r"[qQ]{2,}\s*\d+(?![^\[]*\])",   # QQ号（排除CQ码中的）
    r"加群(?![^\[]*\])",             # 加群邀请（排除CQ码中的）
    r"(vx|wx|weixin)(?![^\[]*\])"    # 微信相关（排除CQ码中的）
}

# CQ码正则表达式，用于匹配图片、表情等特殊消息
CQ_PATTERN = re.compile(r'\[CQ:.*?\]')
# 专门匹配动画表情的CQ码
ANIMATION_EMOJI_PATTERN = re.compile(r'\[CQ:image,summary=&#91;动画表情&#93;.*?\]')

def websocket_lock(func):
    """WebSocket操作锁装饰器，防止并发冲突"""
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        async with self.ws_lock:
            return await func(self, *args, **kwargs)
    return wrapper

class GroupRuleEnforcer:
    def __init__(self):
        self.ban_list: Set[int] = set()
        self.violation_records: Dict[int, Dict[str, int]] = {}  # 用户ID: {"count": 违规次数, "last_time": 最后违规时间}
        self.mute_list: Dict[int, datetime] = {}  # 用户ID: 解禁时间
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.running = True
        self.ws_lock = asyncio.Lock()  # WebSocket操作锁，解决并发问题
        self.commands = {
            "!help": self.show_help,
            "!status": self.show_status,
            "!mute": self.admin_mute,
            "!unmute": self.admin_unmute,
            "!ban": self.admin_ban,
            "!unban": self.admin_unban
        }

    async def connect(self):
        """连接到WebSocket服务器"""
        try:
            headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
            self.websocket = await websockets.connect(
                WS_URL,
                extra_headers=headers,
                ping_interval=30,
                ping_timeout=30,
                close_timeout=10
            )
            logger.info("✅ WebSocket连接成功")
            
            # 订阅必要事件
            await self._send_ws({
                "action": "set_websocket_event",
                "params": {
                    "message": True,
                    "notice": True,
                    "request": True
                }
            })
            return True
        except Exception as e:
            logger.error(f"❌ 连接失败: {str(e)}")
            return False

    async def handle_message(self, event: Dict):
        try:
            message_type = event.get("message_type")
            group_id = event.get("group_id")
            
            # 检查是否在启用的群组中
            if group_id not in ENABLED_GROUPS:
                return

            user_id = event.get("user_id")
            raw_message = event.get("raw_message", "").strip()
            message_id = event.get("message_id")

            # 检查睡觉模式命令
            if raw_message == "启动战云睡觉模式":
                await self.handle_sleep_mode(group_id, user_id, message_id)
                return
                
            # 处理普通命令
            if raw_message.startswith("!"):
                await self.handle_command(event)
                return
                
            if message_type != "group":
                return

            sender = event.get("sender", {})
            sender_role = sender.get("role", "member")

            # 跳过管理人员的消息处理
            if sender_role in ["owner", "admin"]:
                return

            # 检查用户是否在封禁/禁言列表中
            if await self.check_user_status(user_id, group_id):
                return

            # 预处理消息：移除CQ码（表情、图片等）
            processed_message = self._process_message(raw_message)
            
            # 违禁词检测
            await self.check_violation_words(group_id, user_id, processed_message, raw_message, message_id)
            
            # 广告检测
            await self.check_advertisement(group_id, user_id, processed_message, raw_message, message_id)
            
            # 刷屏检测
            await self.check_flood(user_id, group_id, raw_message, message_id)

        except Exception as e:
            logger.error(f"处理消息时出错: {str(e)}")

    def _process_message(self, message: str) -> str:
        """预处理消息：移除CQ码，清理内容用于检测"""
        # 移除所有CQ码
        cleaned = CQ_PATTERN.sub('', message)
        # 移除多余空白
        return re.sub(r'\s+', ' ', cleaned).strip()

    async def handle_sleep_mode(self, group_id: int, user_id: int, message_id: int):
        """处理战云睡觉模式命令"""
        try:
            # 检查发送者权限
            member_info = await self.get_group_member_info(group_id, user_id)
            if member_info.get("role") not in ["owner", "admin"]:
                return

            duration = 8 * 60 * 60  # 8小时
            await self.ban_user(group_id, SLEEP_TARGET_ID, duration)
            
            notice = f"💤 战云睡觉模式已启动\n• 目标用户: {SLEEP_TARGET_ID}\n• 禁言时长: 8小时"
            await self.send_notice(group_id, notice)
            logger.info(f"已启动战云睡觉模式，用户{SLEEP_TARGET_ID}被禁言8小时")
        except Exception as e:
            logger.error(f"启动睡觉模式失败: {str(e)}")

    @websocket_lock
    async def get_group_member_info(self, group_id: int, user_id: int) -> Dict:
        """获取群成员信息"""
        payload = {
            "action": "get_group_member_info",
            "params": {
                "group_id": group_id,
                "user_id": user_id,
                "no_cache": True
            }
        }
        response = await self._send_ws(payload)
        return response.get("data", {})

    async def handle_command(self, event: Dict):
        """处理管理命令"""
        try:
            message = event.get("raw_message", "").strip()
            user_id = event.get("user_id")
            group_id = event.get("group_id")
            
            # 检查是否在启用的群组中
            if group_id not in ENABLED_GROUPS:
                return

            sender = event.get("sender", {})
            sender_role = sender.get("role", "member")

            # 只有管理员可以使用命令
            if sender_role not in ["owner", "admin"]:
                return

            parts = message.split()
            cmd = parts[0].lower()
            
            if cmd in self.commands:
                await self.commands[cmd](group_id, user_id, parts[1:])
                
        except Exception as e:
            logger.error(f"处理命令时出错: {str(e)}")

    async def check_violation_words(self, group_id: int, user_id: int, processed_msg: str, raw_msg: str, message_id: int):
        """违禁词检测"""
        if not processed_msg:  # 空消息不检测
            return
            
        # 三级处罚检测（0容忍词汇）
        if any(re.search(word, processed_msg) for word in LEVEL_3_WORDS):
            logger.warning(f"检测到三级违禁词: 用户{user_id} 消息: {raw_msg[:50]}...")
            await self.enforce_level_3(group_id, user_id, raw_msg, message_id)
            return
        
        # 二级处罚检测
        if any(re.search(word, processed_msg) for word in LEVEL_2_WORDS):
            logger.warning(f"检测到二级违禁词: 用户{user_id} 消息: {raw_msg[:50]}...")
            await self.enforce_level_2(group_id, user_id, message_id)
            return

        # 一级处罚检测
        if any(re.search(word, processed_msg) for word in LEVEL_1_WORDS):
            logger.warning(f"检测到一级违禁词: 用户{user_id} 消息: {raw_msg[:50]}...")
            await self.enforce_level_1(group_id, user_id, message_id)

    async def check_advertisement(self, group_id: int, user_id: int, processed_msg: str, raw_msg: str, message_id: int):
        """广告检测"""
        # 检查是否是纯动画表情消息
        if ANIMATION_EMOJI_PATTERN.fullmatch(raw_msg.strip()):
            return  # 纯动画表情不检测广告
            
        # 空消息（过滤后为空）不检测广告
        if not processed_msg:
            return
            
        if any(re.search(pattern, processed_msg) for pattern in AD_PATTERNS):
            logger.warning(f"检测到广告: 用户{user_id} 消息: {raw_msg[:50]}...")
            await self.enforce_advertisement(group_id, user_id, message_id)

    async def check_flood(self, user_id: int, group_id: int, message: str, message_id: int):
        """刷屏检测"""
        now = datetime.now()
        record = self.violation_records.setdefault(user_id, {"count": 0, "last_time": now, "messages": []})
        
        # 记录最近5条消息
        record["messages"] = record.get("messages", [])[-4:] + [now]
        
        # 5秒内发送超过3条消息视为刷屏
        if len(record["messages"]) >= 3 and (now - record["messages"][0]).total_seconds() < 5:
            logger.warning(f"检测到刷屏: 用户{user_id}")
            await self.enforce_flood(group_id, user_id, message_id)

    async def enforce_level_3(self, group_id: int, user_id: int, message: str, message_id: int):
        """三级处罚：撤回+踢出+拉黑"""
        try:
            tasks = [
                self.delete_message(message_id),
                self.kick_user(group_id, user_id),
                self.ban_user(group_id, user_id, 30*24*60*60)  # 30天黑名单
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            notice = f"🚨 三级处罚执行\n• 用户: {user_id}\n• 违禁词: {message[:50]}...\n• 处理方式: 永久移出"
            await self.send_notice(group_id, notice)
            logger.info(f"已执行三级处罚: 用户{user_id}")
        except Exception as e:
            logger.error(f"执行三级处罚失败: {str(e)}")

    async def enforce_level_2(self, group_id: int, user_id: int, message_id: int):
        """二级处罚：撤回+禁言1天"""
        try:
            await asyncio.gather(
                self.delete_message(message_id),
                self.ban_user(group_id, user_id, 24*60*60),  # 1天禁言
                return_exceptions=True
            )
            self._record_violation(user_id)
            logger.info(f"已执行二级处罚: 用户{user_id}")
        except Exception as e:
            logger.error(f"执行二级处罚失败: {str(e)}")

    async def enforce_level_1(self, group_id: int, user_id: int, message_id: int):
        """一级处罚：撤回+禁言10分钟"""
        try:
            await asyncio.gather(
                self.delete_message(message_id),
                self.ban_user(group_id, user_id, 10*60),  # 10分钟禁言
                return_exceptions=True
            )
            self._record_violation(user_id)
            logger.info(f"已执行一级处罚: 用户{user_id}")
        except Exception as e:
            logger.error(f"执行一级处罚失败: {str(e)}")

    async def enforce_advertisement(self, group_id: int, user_id: int, message_id: int):
        """广告处罚：撤回+禁言1小时"""
        try:
            await asyncio.gather(
                self.delete_message(message_id),
                self.ban_user(group_id, user_id, 60*60),  # 1小时禁言
                return_exceptions=True
            )
            self._record_violation(user_id)
            logger.info(f"已处理广告: 用户{user_id}")
        except Exception as e:
            logger.error(f"处理广告失败: {str(e)}")

    async def enforce_flood(self, group_id: int, user_id: int, message_id: int):
        """刷屏处罚：撤回+禁言30分钟"""
        try:
            await asyncio.gather(
                self.delete_message(message_id),
                self.ban_user(group_id, user_id, 30*60),  # 30分钟禁言
                return_exceptions=True
            )
            self._record_violation(user_id)
            logger.info(f"已处理刷屏: 用户{user_id}")
        except Exception as e:
            logger.error(f"处理刷屏失败: {str(e)}")

    def _record_violation(self, user_id: int):
        """记录违规次数"""
        now = datetime.now()
        record = self.violation_records.setdefault(user_id, {"count": 0, "last_time": now})
        record["count"] += 1
        record["last_time"] = now
        
        if record["count"] >= 3:  # 累计3次自动升级处罚
            self.ban_list.add(user_id)
            logger.warning(f"用户{user_id}违规次数已达3次，加入封禁列表")

    async def check_user_status(self, user_id: int, group_id: int) -> bool:
        """检查用户状态（是否被封禁/禁言）"""
        if user_id in self.ban_list:
            await self.kick_user(group_id, user_id)
            return True
            
        if user_id in self.mute_list and datetime.now() < self.mute_list[user_id]:
            remaining = (self.mute_list[user_id] - datetime.now()).total_seconds()
            if remaining > 0:
                await self.ban_user(group_id, user_id, remaining)
                return True
                
        return False

    async def show_help(self, group_id: int, user_id: int, args: List[str]):
        """显示帮助信息"""
        help_msg = """🤖 管理命令帮助：
!help - 显示本帮助
!status [用户ID] - 查看用户状态
!mute <用户ID> <分钟> - 禁言用户
!unmute <用户ID> - 解除禁言
!ban <用户ID> - 封禁用户
!unban <用户ID> - 解封用户
"启动战云睡觉模式" - 禁言目标用户8小时(仅管理)"""
        await self.send_notice(group_id, help_msg)

    async def show_status(self, group_id: int, user_id: int, args: List[str]):
        """查看用户状态"""
        if not args:
            await self.send_notice(group_id, "❌ 请提供用户ID")
            return
            
        target_id = int(args[0])
        record = self.violation_records.get(target_id, {})
        status = []
        
        if target_id in self.ban_list:
            status.append("🔴 永久封禁")
        elif target_id in self.mute_list:
            remaining = self.mute_list[target_id] - datetime.now()
            if remaining.total_seconds() > 0:
                status.append(f"🟡 禁言中（剩余{remaining.seconds//60}分钟）")
            else:
                del self.mute_list[target_id]
                
        status.append(f"违规次数: {record.get('count', 0)}次")
        status.append(f"最后违规: {record.get('last_time', '无记录')}")
        
        await self.send_notice(group_id, f"用户 {target_id} 状态:\n" + "\n".join(status))

    async def admin_mute(self, group_id: int, user_id: int, args: List[str]):
        """管理员禁言"""
        if len(args) < 2:
            await self.send_notice(group_id, "❌ 用法: !mute <用户ID> <分钟>")
            return
            
        target_id = int(args[0])
        minutes = int(args[1])
        
        await self.ban_user(group_id, target_id, minutes * 60)
        self.mute_list[target_id] = datetime.now() + timedelta(minutes=minutes)
        await self.send_notice(group_id, f"✅ 已禁言用户 {target_id} {minutes}分钟")

    async def admin_unmute(self, group_id: int, user_id: int, args: List[str]):
        """管理员解除禁言"""
        if not args:
            await self.send_notice(group_id, "❌ 请提供用户ID")
            return
            
        target_id = int(args[0])
        
        if target_id in self.mute_list:
            del self.mute_list[target_id]
            await self.ban_user(group_id, target_id, 0)  # 解除禁言
            await self.send_notice(group_id, f"✅ 已解除用户 {target_id} 的禁言")
        else:
            await self.send_notice(group_id, f"⚠️ 用户 {target_id} 未被禁言")

    async def admin_ban(self, group_id: int, user_id: int, args: List[str]):
        """管理员封禁"""
        if not args:
            await self.send_notice(group_id, "❌ 请提供用户ID")
            return
            
        target_id = int(args[0])
        self.ban_list.add(target_id)
        await self.kick_user(group_id, target_id)
        await self.send_notice(group_id, f"✅ 已封禁用户 {target_id}")

    async def admin_unban(self, group_id: int, user_id: int, args: List[str]):
        """管理员解封"""
        if not args:
            await self.send_notice(group_id, "❌ 请提供用户ID")
            return
            
        target_id = int(args[0])
        
        if target_id in self.ban_list:
            self.ban_list.remove(target_id)
            await self.send_notice(group_id, f"✅ 已解封用户 {target_id}")
        else:
            await self.send_notice(group_id, f"⚠️ 用户 {target_id} 未被封禁")

    @websocket_lock
    async def delete_message(self, message_id: int):
        """撤回消息"""
        payload = {
            "action": "delete_msg",
            "params": {
                "message_id": message_id
            }
        }
        return await self._send_ws(payload)

    @websocket_lock
    async def ban_user(self, group_id: int, user_id: int, duration: int):
        """禁言用户"""
        payload = {
            "action": "set_group_ban",
            "params": {
                "group_id": group_id,
                "user_id": user_id,
                "duration": duration
            }
        }
        return await self._send_ws(payload)

    @websocket_lock
    async def kick_user(self, group_id: int, user_id: int):
        """踢出用户"""
        payload = {
            "action": "set_group_kick",
            "params": {
                "group_id": group_id,
                "user_id": user_id,
                "reject_add_request": True
            }
        }
        return await self._send_ws(payload)

    @websocket_lock
    async def send_notice(self, group_id: int, text: str):
        """发送通知消息"""
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": text
            }
        }
        return await self._send_ws(payload)

    async def _send_ws(self, payload: Dict):
        """发送WebSocket请求"""
        try:
            if not self.websocket:
                raise ConnectionError("WebSocket连接未建立")
            
            await self.websocket.send(json.dumps(payload))
            response = await self.websocket.recv()
            logger.debug(f"API响应: {response}")
            return json.loads(response)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("连接已关闭，尝试重连...")
            await self.reconnect()
            raise
        except Exception as e:
            logger.error(f"发送WS请求失败: {str(e)}")
            raise

    async def reconnect(self):
        """重新连接"""
        if self.websocket:
            await self.websocket.close()
        return await self.connect()

    async def run(self):
        """主运行循环"""
        while self.running:
            try:
                if not await self.connect():
                    await asyncio.sleep(5)
                    continue

                logger.info("🚀 机器人已启动，等待消息...")
                async for message in self.websocket:
                    try:
                        event = json.loads(message)
                        logger.debug(f"收到原始事件: {event}")
                        if event.get("post_type") == "message":
                            await self.handle_message(event)
                    except json.JSONDecodeError:
                        logger.error(f"无法解析的消息: {message}")
                    except Exception as e:
                        logger.error(f"处理消息时出错: {str(e)}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("⚠️ 连接断开，5秒后尝试重连...")
                await asyncio.sleep(5)
            except KeyboardInterrupt:
                logger.info("收到终止信号，准备退出...")
                break
            except Exception as e:
                logger.error(f"运行时错误: {str(e)}")
                await asyncio.sleep(10)
            finally:
                if self.websocket:
                    await self.websocket.close()

    async def shutdown(self):
        """关闭机器人"""
        self.running = False
        if self.websocket:
            await self.websocket.close()

async def main():
    bot = GroupRuleEnforcer()
    try:
        await bot.run()
    except KeyboardInterrupt:
        await bot.shutdown()
    except Exception as e:
        logger.critical(f"致命错误: {str(e)}")
    finally:
        logger.info("机器人已停止")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已终止")
    except Exception as e:
        logger.critical(f"未捕获的异常: {str(e)}")
    finally:
        input("按回车键退出...")
