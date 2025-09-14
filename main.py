import asyncio
import json
import websockets
import re
from typing import Dict, Set, Optional, List
import logging
from datetime import datetime, timedelta
from functools import wraps
import aiohttp

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
WS_URL = "ws://这不能说喵自己改喵:这不能说喵自己改喵"
ACCESS_TOKEN = "这不能说喵自己改喵"
ADMIN_GROUP_ID = 923820685
SLEEP_TARGET_ID = 1724270068  # 战云用户ID

# 点赞相关配置
LIKE_COOLDOWN_HOURS = 24  # 冷却时间（小时）
LIKE_COUNT = 10  # 每次点赞数量

# Minecraft服务器配置
MC_SERVERS = {
    "主服": {"host": "mc.tzi998.com", "port": 25565},
    "模组服": {"host": "mod.tzi998.com", "port": 25565},
    # 可以添加更多服务器
}

# 服务器状态监控配置
SERVER_CHECK_INTERVAL = 300  # 5分钟检查一次
SERVER_CHECK_RETRY = 3  # 离线检测重试次数
SERVER_CHECK_TIMEOUT = 15  # 服务器查询超时时间（秒）

# 启用的群组列表（只有在这些群中才会启用bot）
ENABLED_GROUPS = {
    923820685,  # 主群
    1022514126   # 备用群
}

# 违禁词库（支持正则表达式）
LEVEL_3_WORDS = {r"kukemc", r"kuke", r"酷可", r"kamu", r"咖目"}  # 直接踢出
LEVEL_2_WORDS = {r"以色列", r"女大", r"特朗普"}                   # 禁言1天 
LEVEL_1_WORDS = {r"傻[逼屄]", r"脑残", r"死妈"}                # 禁言10分钟

# 广告检测规则 - 优化版本
AD_PATTERNS = {
    # 排除CQ码中的内容，避免匹配表情/图片中的参数
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

class MinecraftServerStatus:
    """Minecraft服务器状态查询类 - 简化版本"""
    
    @staticmethod
    async def query_server(host: str, port: int = 25565) -> dict:
        """查询Minecraft服务器状态 - 使用可靠的API"""
        try:
            # 使用可靠的API端点
            api_urls = [
                f"https://api.mcsrvstat.us/3/{host}:{port}",
                f"https://api.mcsrvstat.us/2/{host}:{port}",
                f"https://api.mcsrvstat.us/simple/{host}:{port}",
                f"https://api.mcstatus.io/v2/status/java/{host}:{port}",
            ]
            
            async with aiohttp.ClientSession() as session:
                for api_url in api_urls:
                    try:
                        logger.debug(f"尝试API: {api_url}")
                        async with session.get(api_url, timeout=SERVER_CHECK_TIMEOUT) as response:
                            if response.status == 200:
                                data = await response.json()
                                
                                # 处理不同的API响应格式
                                if 'mcsrvstat.us' in api_url:
                                    if data.get("online", False):
                                        return {
                                            "online": True,
                                            "players": {
                                                "online": data.get("players", {}).get("online", 0),
                                                "max": data.get("players", {}).get("max", 0)
                                            },
                                            "version": data.get("version", "未知"),
                                            "motd": data.get("motd", {}).get("clean", ["未知"])[0] if isinstance(data.get("motd"), dict) else "未知"
                                        }
                                elif 'mcstatus.io' in api_url:
                                    if data.get("online", False):
                                        return {
                                            "online": True,
                                            "players": {
                                                "online": data.get("players", {}).get("online", 0),
                                                "max": data.get("players", {}).get("max", 0)
                                            },
                                            "version": data.get("version", {}).get("name_raw", "未知"),
                                            "motd": data.get("motd", {}).get("raw", "未知")
                                        }
                                
                    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                        logger.debug(f"API {api_url} 查询失败: {str(e)}")
                        continue
            
            # 如果所有API都失败，尝试直接连接端口
            try:
                logger.debug(f"尝试直接连接: {host}:{port}")
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=10
                )
                writer.close()
                await writer.wait_closed()
                return {
                    "online": True,
                    "players": {"online": 0, "max": 0},
                    "version": "未知（端口可连接）",
                    "motd": "端口可连接但协议查询失败"
                }
            except:
                pass
                        
        except Exception as e:
            logger.debug(f"服务器查询完全失败 {host}:{port}: {str(e)}")
        
        return {"online": False, "players": {"online": 0, "max": 0}, "version": "未知"}

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
            "!unban": self.admin_unban,
            "!mcstatus": self.check_mc_status  # 新增：MC服务器状态命令
        }
        # 新增：点赞冷却时间存储（用户ID: 上次点赞时间）
        self.like_cooldowns: Dict[int, datetime] = {}
        
        # 新增：服务器状态监控
        self.server_status: Dict[str, bool] = {}  # 服务器名称: 是否在线
        self.server_retry_count: Dict[str, int] = {}  # 服务器名称: 重试次数
        self.monitor_task = None  # 服务器监控任务

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
            
            # 启动服务器状态监控
            self.monitor_task = asyncio.create_task(self.monitor_servers())
            
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

            # 新增：处理点赞请求（放在其他命令处理前面）
            if raw_message == "赞我":
                await self.handle_like_request(group_id, user_id)
                return
                
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

    # 新增：处理点赞请求
    async def handle_like_request(self, group_id: int, user_id: int):
        """处理用户的点赞请求"""
        try:
            # 检查是否在冷却期内
            now = datetime.now()
            last_liked = self.like_cooldowns.get(user_id)
            
            if last_liked and (now - last_liked).total_seconds() < LIKE_COOLDOWN_HOURS * 3600:
                remaining_hours = (LIKE_COOLDOWN_HOURS * 3600 - (now - last_liked).total_seconds()) / 3600
                await self.send_notice(group_id, f"⏳ 点赞功能冷却中，请{int(remaining_hours)}小时后再试")
                return
            
            # 执行点赞操作
            success = await self.send_likes(user_id, LIKE_COUNT)
            
            if success:
                # 更新冷却时间
                self.like_cooldowns[user_id] = now
                await self.send_notice(group_id, f"👍 已为用户{user_id}送上{LIKE_COUNT}个赞！")
                logger.info(f"已为用户{user_id}点赞{LIKE_COUNT}次")
            else:
                await self.send_notice(group_id, "❌ 点赞失败，请稍后再试")
                
        except Exception as e:
            logger.error(f"处理点赞请求失败: {str(e)}")
            await self.send_notice(group_id, "❌ 点赞过程中出现错误")

    # 新增：发送点赞
    @websocket_lock
    async def send_likes(self, user_id: int, count: int) -> bool:
        """通过WebSocket发送点赞"""
        try:
            # 发送点赞的API请求
            payload = {
                "action": "send_like",
                "params": {
                    "user_id": user_id,
                    "times": count
                }
            }
            
            response = await self._send_ws(payload)
            # 根据接口返回判断是否成功
            return response.get("status") == "ok" or response.get("retcode") == 0
            
        except Exception as e:
            logger.error(f"发送点赞失败: {str(e)}")
            return False

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

    # 新增：处理MC服务器状态查询
    async def check_mc_status(self, group_id: int, user_id: int, args: List[str]):
        """查询Minecraft服务器状态"""
        try:
            if not args:
                # 如果没有指定服务器，显示所有服务器状态
                status_messages = []
                for server_name, server_config in MC_SERVERS.items():
                    # 使用更可靠的查询方法
                    status_data = await self._reliable_server_query(server_config["host"], server_config["port"])
                    status_emoji = "🟢" if status_data["online"] else "🔴"
                    status_text = f"{status_emoji} {server_name}: {server_config['host']}"
                    if status_data["online"]:
                        status_text += f"\n  玩家: {status_data['players']['online']}/{status_data['players']['max']} | 版本: {status_data['version']}"
                    else:
                        status_text += " | 离线"
                    status_messages.append(status_text)
                
                await self.send_notice(group_id, "🎮 Minecraft服务器状态:\n" + "\n".join(status_messages))
                return
                
            # 查询指定服务器
            server_name = args[0]
            if server_name not in MC_SERVERS:
                await self.send_notice(group_id, f"❌ 未知服务器: {server_name}\n可用服务器: {', '.join(MC_SERVERS.keys())}")
                return
                
            server_config = MC_SERVERS[server_name]
            # 使用更可靠的查询方法
            status_data = await self._reliable_server_query(server_config["host"], server_config["port"])
            
            if status_data["online"]:
                status_msg = (f"🟢 {server_name} 服务器在线\n"
                             f"• 地址: {server_config['host']}:{server_config['port']}\n"
                             f"• 玩家: {status_data['players']['online']}/{status_data['players']['max']}\n"
                             f"• 版本: {status_data['version']}")
                if status_data.get('motd'):
                    status_msg += f"\n• MOTD: {status_data['motd']}"
            else:
                status_msg = (f"🔴 {server_name} 服务器离线\n"
                             f"• 地址: {server_config['host']}:{server_config['port']}\n"
                             f"• 状态: 无法连接")
                
            await self.send_notice(group_id, status_msg)
            
        except Exception as e:
            logger.error(f"查询MC服务器状态失败: {str(e)}")
            await self.send_notice(group_id, "❌ 查询服务器状态时出错")

    async def _reliable_server_query(self, host: str, port: int) -> dict:
        """更可靠的服务器查询方法，包含重试机制"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = await MinecraftServerStatus.query_server(host, port)
                logger.info(f"服务器 {host}:{port} 查询结果: {'在线' if result['online'] else '离线'} (尝试 {attempt + 1})")
                return result
            except Exception as e:
                logger.warning(f"服务器查询尝试 {attempt + 1} 失败: {str(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)  # 等待1秒后重试
        
        # 所有尝试都失败，返回离线状态
        return {"online": False, "players": {"online": 0, "max": 0}, "version": "未知"}

    # 新增：监控服务器状态
    async def monitor_servers(self):
        """监控所有Minecraft服务器状态"""
        # 初始状态设为在线，避免启动时误报
        for server_name in MC_SERVERS.keys():
            self.server_status[server_name] = True
            self.server_retry_count[server_name] = 0
        
        logger.info("🔄 开始监控Minecraft服务器状态")
        
        while self.running:
            try:
                for server_name, server_config in MC_SERVERS.items():
                    # 使用更可靠的查询方法
                    status_data = await self._reliable_server_query(server_config["host"], server_config["port"])
                    is_online = status_data["online"]
                    previous_status = self.server_status.get(server_name, True)
                    
                    logger.info(f"服务器 {server_name} 状态: {'在线' if is_online else '离线'} (之前: {'在线' if previous_status else '离线'})")
                    
                    # 如果状态变化
                    if is_online != previous_status:
                        if not is_online:
                            # 服务器离线，增加重试计数
                            retry_count = self.server_retry_count.get(server_name, 0) + 1
                            self.server_retry_count[server_name] = retry_count
                            
                            logger.info(f"服务器 {server_name} 离线检测 #{retry_count}")
                            
                            # 只有多次检测到离线才认为是真的离线
                            if retry_count >= SERVER_CHECK_RETRY:
                                self.server_status[server_name] = False
                                await self.notify_server_status(server_name, False)
                        else:
                            # 服务器恢复在线
                            self.server_status[server_name] = True
                            self.server_retry_count[server_name] = 0
                            await self.notify_server_status(server_name, True)
                    else:
                        # 状态未变化，重置重试计数
                        self.server_retry_count[server_name] = 0
                
                # 等待下一次检查
                logger.debug(f"等待 {SERVER_CHECK_INTERVAL} 秒后进行下一次服务器检查")
                await asyncio.sleep(SERVER_CHECK_INTERVAL)
                
            except Exception as e:
                logger.error(f"服务器监控出错: {str(e)}")
                await asyncio.sleep(SERVER_CHECK_INTERVAL)

    # 新增：通知服务器状态变化
    async def notify_server_status(self, server_name: str, is_online: bool):
        """通知服务器状态变化"""
        try:
            server_config = MC_SERVERS[server_name]
            if is_online:
                # 获取详细的服务器信息
                status_data = await self._reliable_server_query(server_config["host"], server_config["port"])
                message = (f"[🟢Online]服务器 {server_name} 已恢复在线\n"
                          f"• 地址: {server_config['host']}:{server_config['port']}\n"
                          f"• 玩家: {status_data['players']['online']}/{status_data['players']['max']}")
            else:
                message = (f"[🔴Offline]服务器 {server_name} 貌似离线了\n"
                          f"• 地址: {server_config['host']}:{server_config['port']}\n"
                          f"• 已尝试检测 {SERVER_CHECK_RETRY} 次确认")
            
            # 在所有启用的群组中发送通知
            for group_id in ENABLED_GROUPS:
                await self.send_notice(group_id, message)
                
            logger.info(f"服务器状态通知: {server_name} {'在线' if is_online else '离线'}")
        except Exception as e:
            logger.error(f"发送服务器状态通知失败: {str(e)}")

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

    # 新增：显示帮助信息
    async def show_help(self, group_id: int, user_id: int, args: List[str]):
        """显示帮助信息"""
        help_msg = """🤖 管理命令帮助：
!help - 显示本帮助
!status [用户ID] - 查看用户状态
!mute <用户ID> <分钟> - 禁言用户
!unmute <用户ID> - 解除禁言
!ban <用户ID> - 封禁用户
!unban <用户ID> - 解封用户
!mcstatus [服务器名] - 查看MC服务器状态
"启动战云睡觉模式" - 禁言目标用户8小时(仅管理)
"赞我" - 获取10个赞（每天一次）"""
        await self.send_notice(group_id, help_msg)

    # 新增：查看用户状态
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
                
        # 新增：显示点赞冷却状态
        if target_id in self.like_cooldowns:
            last_liked = self.like_cooldowns[target_id]
            if (datetime.now() - last_liked).total_seconds() < LIKE_COOLDOWN_HOURS * 3600:
                remaining = (LIKE_COOLDOWN_HOURS * 3600 - (datetime.now() - last_liked).total_seconds()) / 3600
                status.append(f"👍 点赞冷却中（剩余{int(remaining)}小时）")
            else:
                status.append("👍 点赞功能可用")
        else:
            status.append("👍 点赞功能可用")
                
        status.append(f"违规次数: {record.get('count', 0)}次")
        status.append(f"最后违规: {record.get('last_time', '无记录')}")
        
        await self.send_notice(group_id, f"用户 {target_id} 状态:\n" + "\n".join(status))

    # 新增：管理员禁言
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

    # 新增：管理员解除禁言
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

    # 新增：管理员封禁
    async def admin_ban(self, group_id: int, user_id: int, args: List[str]):
        """管理员封禁"""
        if not args:
            await self.send_notice(group_id, "❌ 请提供用户ID")
            return
            
        target_id = int(args[0])
        self.ban_list.add(target_id)
        await self.kick_user(group_id, target_id)
        await self.send_notice(group_id, f"✅ 已封禁用户 {target_id}")

    # 新增：管理员解封
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
        if self.monitor_task:
            self.monitor_task.cancel()

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
