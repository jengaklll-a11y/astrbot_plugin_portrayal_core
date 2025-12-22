from astrbot.api.event import filter
from astrbot.api.star import Context, Star
# 修正：AstrBotConfig 需要从 core 模块导入
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    def _get_target_info(self, event: AiocqhttpMessageEvent):
        """解析目标用户ID (从At或发送者)"""
        for seg in event.get_messages():
            if isinstance(seg, At) and str(seg.qq) != event.get_self_id():
                return str(seg.qq)
        return event.get_sender_id()

    async def _get_user_nickname_gender(self, event: AiocqhttpMessageEvent, user_id: str):
        """获取昵称和性别"""
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(user_id)
            )
            return info.get("card") or info.get("nickname") or "群友", info.get("sex", "unknown")
        except Exception:
            return "群友", "unknown"

    async def _fetch_user_history(self, event: AiocqhttpMessageEvent, target_id: str, max_rounds: int):
        """核心：循环拉取历史消息并过滤出目标用户的纯文本"""
        contexts = []
        message_seq = 0
        group_id = event.get_group_id()
        
        # 将配置的 float/str 转为 int，确保安全
        max_msg_limit = int(self.config.get("max_msg_count", 500))

        for _ in range(max_rounds):
            if len(contexts) >= max_msg_limit:
                break

            payload = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": 100, # 每次拉取100条
            }
            try:
                # 适配部分非OneBot标准的实现，尝试不同参数
                result = await event.bot.api.call_action("get_group_msg_history", **payload)
                messages = result.get("messages", [])
            except Exception as e:
                logger.warning(f"拉取历史消息失败: {e}")
                break

            if not messages:
                break
            
            # 更新 seq 以便下次拉取更早的消息
            message_seq = messages[0]["message_id"]

            # 倒序遍历（从新到旧），提取目标用户的文本
            for msg in messages:
                if str(msg["sender"]["user_id"]) != target_id:
                    continue
                
                # 提取纯文本部分
                text_content = "".join([
                    seg["data"]["text"] 
                    for seg in msg["message"] 
                    if seg["type"] == "text"
                ]).strip()

                if text_content:
                    contexts.append({"role": "user", "content": text_content})

        return contexts

    @filter.command("画像")
    async def generate_portrayal(self, event: AiocqhttpMessageEvent):
        """指令入口"""
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("❌ 未配置 LLM 服务，无法分析。")
            return

        # 1. 确定目标
        target_id = self._get_target_info(event)
        nickname, gender = await self._get_user_nickname_gender(event, target_id)
        
        # 2. 解析可选的轮数参数
        args = event.message_str.split()
        rounds = int(args[-1]) if args and args[-1].isdigit() else self.config.get("max_query_rounds", 20)
        rounds = min(50, max(1, rounds)) # 限制范围 1-50

        yield event.plain_result(f"🔍 正在回溯 {nickname} 的最近消息 (最大{rounds}轮)...")

        # 3. 获取数据
        history = await self._fetch_user_history(event, target_id, rounds)
        
        if not history:
            yield event.plain_result(f"⚠️ 未找到 {nickname} 的有效发言记录。")
            return

        yield event.plain_result(f"✅ 收集到 {len(history)} 条发言，正在构建画像...")

        # 4. 构建提示词并请求 LLM
        gender_cn = "他" if gender == "male" else ("她" if gender == "female" else "TA")
        system_prompt = self.config.get("system_prompt_template", "").format(
            nickname=nickname, gender=gender_cn
        )
        
        try:
            response = await provider.text_chat(
                prompt=f"以下是 {nickname} 的聊天记录，请根据 System Prompt 要求进行分析：",
                system_prompt=system_prompt,
                contexts=history  # 这里直接传入 list[dict]
            )
            
            # 5. 输出结果 (Markdown 格式)
            yield event.plain_result(response.completion_text)
            
        except Exception as e:
            logger.error(f"画像生成失败: {e}")
            yield event.plain_result(f"❌ 分析过程中发生错误: {e}")
