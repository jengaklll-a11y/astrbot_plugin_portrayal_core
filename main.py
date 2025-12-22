from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
# 引入标准消息组件
from astrbot.api.message_components import At, Reply, Image, Plain
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
        max_msg_limit = int(self.config.get("max_msg_count", 500))

        for _ in range(max_rounds):
            if len(contexts) >= max_msg_limit:
                break
            payload = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": 100,
            }
            try:
                result = await event.bot.api.call_action("get_group_msg_history", **payload)
                messages = result.get("messages", [])
            except Exception as e:
                break
            if not messages:
                break
            message_seq = messages[0]["message_id"]
            for msg in messages:
                if str(msg["sender"]["user_id"]) != target_id:
                    continue
                text_content = "".join([seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"]).strip()
                if text_content:
                    contexts.append({"role": "user", "content": text_content})
        return contexts

    def _force_find_provider(self, target_id: str):
        """深度查找 Provider"""
        if not target_id: return None
        target_id_lower = target_id.lower()
        
        all_providers = []
        if hasattr(self.context, "register"):
            reg_providers = getattr(self.context.register, "providers", None)
            if isinstance(reg_providers, dict):
                all_providers.extend(reg_providers.values())
            elif isinstance(reg_providers, list):
                all_providers.extend(reg_providers)
        
        if hasattr(self.context, "get_all_providers"):
            try:
                all_providers.extend(self.context.get_all_providers())
            except Exception: pass

        seen = set()
        for p in all_providers:
            if not p or id(p) in seen: continue
            seen.add(id(p))
            
            p_ids = []
            if hasattr(p, "id") and p.id: p_ids.append(str(p.id))
            if hasattr(p, "provider_id") and p.provider_id: p_ids.append(str(p.provider_id))
            if hasattr(p, "config") and isinstance(p.config, dict) and p.config.get("id"): 
                p_ids.append(str(p.config["id"]))
            if hasattr(p, "provider_config") and isinstance(p.provider_config, dict) and p.provider_config.get("id"): 
                p_ids.append(str(p.provider_config["id"]))

            for pid in p_ids:
                if pid.lower() == target_id_lower:
                    return p
        return None

    @filter.command("画像")
    async def generate_portrayal(self, event: AiocqhttpMessageEvent):
        """指令入口"""
        provider = None
        cfg_provider_id = self.config.get("llm_provider_id")
        
        if cfg_provider_id:
            provider = self._force_find_provider(cfg_provider_id)
        
        if not provider:
            if cfg_provider_id:
                logger.warning(f"Portrayal: 指定模型 '{cfg_provider_id}' 未找到，使用默认模型。")
            provider = self.context.get_using_provider()
            
        if not provider:
            yield event.plain_result("❌ 未找到可用的 LLM 服务。")
            return

        curr_id = getattr(provider, "id", None) or getattr(provider, "provider_id", None) or type(provider).__name__
        logger.info(f"Portrayal: 使用模型 [{curr_id}] 为用户画像")

        target_id = self._get_target_info(event)
        nickname, gender = await self._get_user_nickname_gender(event, target_id)
        
        args = event.message_str.split()
        rounds = int(args[-1]) if args and args[-1].isdigit() else self.config.get("max_query_rounds", 20)
        rounds = min(50, max(1, rounds))

        yield event.plain_result(f"🔍 正在回溯 {nickname} 的最近消息并构建画像，请稍候...")

        history = await self._fetch_user_history(event, target_id, rounds)
        if not history:
            yield event.plain_result(f"⚠️ 未找到 {nickname} 的有效发言记录。")
            return
        
        logger.info(f"Portrayal: 收集到 {len(history)} 条发言")

        gender_cn = "他" if gender == "male" else ("她" if gender == "female" else "TA")
        system_prompt = self.config.get("system_prompt_template", "").format(
            nickname=nickname, gender=gender_cn
        )
        
        try:
            response = await provider.text_chat(
                prompt=f"以下是 {nickname} 的聊天记录，请根据 System Prompt 要求进行分析：",
                system_prompt=system_prompt,
                contexts=history
            )
            
            result_text = response.completion_text
            enable_image = self.config.get("enable_image_output", False)
            
            sent_success = False
            
            if enable_image:
                try:
                    img_result = await self.text_to_image(result_text)
                    
                    if img_result:
                        chain = []
                        # 1. 引用原文 (保留)
                        if hasattr(event.message_obj, "message_id"):
                            chain.append(Reply(id=event.message_obj.message_id))
                        
                        # 2. 艾特发送者 (已移除)
                        # chain.append(At(qq=event.get_sender_id())) 
                        
                        # 3. 图片 (兼容 URL 和 本地路径)
                        if str(img_result).startswith("http"):
                            chain.append(Image.fromURL(img_result))
                        else:
                            chain.append(Image.fromFileSystem(img_result))
                        
                        yield event.chain_result(chain)
                        sent_success = True
                    else:
                        logger.warning("Portrayal: 图片生成返回为空，转为纯文本发送。")
                except Exception as e:
                    logger.error(f"Portrayal: 图片构建或发送失败: {e}，正在尝试回退到纯文本模式。")
            
            if not sent_success:
                yield event.plain_result(result_text)

        except Exception as e:
            logger.error(f"画像生成失败: {e}")
            yield event.plain_result(f"❌ 分析过程中发生错误: {e}")
