import asyncio
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
from astrbot.api.message_components import At, Reply, Image, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.texts_cache: dict[str, list[str]] = {}

    def _get_target_info(self, event: AiocqhttpMessageEvent):
        """解析目标用户ID"""
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

    # ================= 历史抓取逻辑 (已修复死循环问题) =================

    async def _fetch_next_batch_robust(self, client, group_id, cursor_seq, error_strike_ref):
        """[底层] 获取单批次消息 (防1200错误 + 指数跳跃 + 动态Batch + 熔断机制)"""
        batch_size = self.config.get("batch_size", 100)
        
        # --- [修复] 新增熔断检查：防止无限重试 ---
        MAX_RETRY_STRIKE = 15 
        if error_strike_ref[0] > MAX_RETRY_STRIKE:
            logger.error(f"Portrayal: 连续失败次数过多 ({error_strike_ref[0]}次)，触发熔断停止回溯，避免死循环。")
            # 返回 0 让上层 _fetch_user_history_smart 退出循环
            return [], 0, False 
        # --------------------------------------

        try:
            payload = {
                "group_id": int(group_id),
                "count": batch_size,
                "reverseOrder": True
            }
            if cursor_seq > 0:
                payload["message_seq"] = cursor_seq

            res = await client.api.call_action("get_group_msg_history", **payload)
            
            if not res or not isinstance(res, dict): return [], 0, False
            batch = res.get("messages", [])
            if not batch: return [], 0, True 
            
            oldest_msg = batch[0]
            next_cursor = int(oldest_msg.get("message_seq") or oldest_msg.get("message_id") or 0)
            
            # 如果成功获取，重置错误计数器
            if error_strike_ref[0] > 0:
                error_strike_ref[0] = 0
                
            return batch, next_cursor, True

        except Exception as e:
            err_msg = str(e)
            # 处理 1200 错误或消息不存在的情况
            if "1200" in err_msg or "不存在" in err_msg:
                error_strike_ref[0] += 1
                current_strike = error_strike_ref[0]
                
                base_jump = max(50, batch_size) 
                # 限制指数最大倍数，防止溢出
                jump_step = base_jump * (2 ** (min(current_strike, 8) - 1))
                
                # 仅在前几次或每5次打印一次警告，减少日志刷屏
                if current_strike <= 5 or current_strike % 5 == 0:
                    logger.warning(f"Portrayal: 游标 {cursor_seq} 处断层 (重试 {current_strike}/{MAX_RETRY_STRIKE} 次)，尝试向下跳跃 {jump_step} 条...")
                
                new_cursor = cursor_seq - jump_step
                return [], new_cursor, False 
            else:
                logger.warning(f"Portrayal: API请求中断: {e}")
                # 遇到其他未知错误，停止尝试，防止死循环
                return [], 0, False

    async def _fetch_user_history_smart(self, event: AiocqhttpMessageEvent, target_id: str, max_rounds: int):
        """[上层] 深度优先抓取：固定拉取 max_rounds 轮"""
        group_id = event.get_group_id()
        
        collected_texts = []
        cursor_seq = 0
        error_strike = [0] 
        real_rounds = 0
        
        while real_rounds < max_rounds:
            batch, next_cursor, success = await self._fetch_next_batch_robust(
                event.bot, group_id, cursor_seq, error_strike
            )
            
            if not success:
                # 如果返回的 next_cursor <= 0，说明到底了或者触发了熔断，直接退出
                if next_cursor <= 0: break
                cursor_seq = next_cursor
                await asyncio.sleep(0.1)
                continue
            
            if not batch: break
                
            for msg in reversed(batch): 
                if str(msg["sender"]["user_id"]) != target_id: continue
                try:
                    msg_content = msg.get("message", [])
                    text = ""
                    if isinstance(msg_content, str): text = msg_content
                    else: text = "".join([s["data"]["text"] for s in msg_content if s.get("type") == "text"])
                    
                    if text.strip(): 
                        collected_texts.append(text.strip())
                except: continue

            cursor_seq = next_cursor
            real_rounds += 1
            await asyncio.sleep(0.2) 

        return collected_texts[::-1], real_rounds

    # ================= Provider 查找逻辑 =================

    def _force_find_provider(self, target_id: str):
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
                logger.warning(f"Portrayal: 指定模型 '{cfg_provider_id}' 未找到，正在尝试使用默认模型。")
            if hasattr(event, "unified_msg_origin"):
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            
        if not provider:
            yield event.plain_result("❌ 未找到可用的 LLM 服务。")
            return

        curr_id = getattr(provider, "id", None) or getattr(provider, "provider_id", None) or type(provider).__name__
        logger.info(f"Portrayal: 使用模型 [{curr_id}] 为用户画像")

        target_id = self._get_target_info(event)
        nickname, gender = await self._get_user_nickname_gender(event, target_id)
        
        args = event.message_str.split()
        custom_rounds = None
        force_refresh = False
        for arg in args:
            if arg.isdigit(): custom_rounds = int(arg)
            if "更新" in arg or "刷新" in arg: force_refresh = True
            
        max_rounds = custom_rounds if custom_rounds else self.config.get("max_query_rounds", 20)
        max_rounds = min(100, max(1, max_rounds))
        
        batch_size = self.config.get("batch_size", 100)
        total_raw_msgs = max_rounds * batch_size

        texts = []
        # 准备一个变量来存储“回溯结束”的文案，暂不发送
        completion_text = ""

        if not force_refresh and target_id in self.texts_cache:
            texts = self.texts_cache[target_id]
            completion_text = f"✅ 从缓存加载：找到了 {len(texts)} 条有效发言。"
        else:
            yield event.plain_result(f"🔍 正在深度回溯 {nickname} 的最近消息 (深度: {max_rounds}轮 / 约{total_raw_msgs}条)...")
            texts, rounds_done = await self._fetch_user_history_smart(event, target_id, max_rounds)
            if texts:
                self.texts_cache[target_id] = texts
                completion_text = f"✅ 回溯结束：在 {rounds_done} 轮中找到了 {len(texts)} 条有效发言。"

        if not texts or len(texts) < 3:
            yield event.plain_result(f"⚠️ {nickname} 的发言太少了（仅 {len(texts)} 条），无法生成准确画像。")
            return

        gender_cn = "他" if gender == "male" else ("她" if gender == "female" else "TA")
        system_prompt = self.config.get("system_prompt_template", "").format(
            nickname=nickname, gender=gender_cn
        )
        
        try:
            context_payload = [{"role": "user", "content": t} for t in texts]
            
            response = await provider.text_chat(
                prompt=f"以下是 {nickname} 的聊天记录，请根据 System Prompt 要求进行分析：",
                system_prompt=system_prompt,
                contexts=context_payload
            )
            
            result_text = response.completion_text
            enable_image = self.config.get("enable_image_output", False)
            sent_success = False
            
            if enable_image:
                try:
                    img_result = None
                    if hasattr(self, "text_to_image"): img_result = await self.text_to_image(result_text)
                    elif hasattr(self.context, "text_to_image"): img_result = await self.context.text_to_image(result_text)
                    
                    if img_result:
                        chain = []
                        # 1. 引用原文
                        if hasattr(event.message_obj, "message_id"): 
                            chain.append(Reply(id=event.message_obj.message_id))
                        
                        # 2. 插入回溯结束的文案
                        if completion_text:
                            chain.append(Plain(completion_text + "\n"))

                        # 3. 插入图片
                        if str(img_result).startswith("http"): chain.append(Image.fromURL(img_result))
                        else: chain.append(Image.fromFileSystem(img_result))
                        
                        yield event.chain_result(chain)
                        sent_success = True
                except Exception as e:
                    logger.warning(f"Portrayal: 转图失败 {e}，回退文本")
            
            if not sent_success:
                # 纯文本模式下，也带上回溯结束的文案
                final_msg = f"{completion_text}\n\n{result_text}" if completion_text else result_text
                yield event.plain_result(final_msg)

        except Exception as e:
            logger.error(f"画像生成失败: {e}")
            yield event.plain_result(f"❌ 分析过程中发生错误: {e}")
