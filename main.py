import asyncio
from collections import OrderedDict
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
        # [修复] 使用 OrderedDict 并限制大小，防止内存泄漏
        self.texts_cache: OrderedDict[str, list[str]] = OrderedDict()
        self.MAX_CACHE_SIZE = 50 

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

    # ================= 历史抓取逻辑 (已修复死循环与指针问题) =================

    async def _fetch_next_batch_robust(self, client, group_id, cursor_seq, current_strike):
        """
        [底层] 获取单批次消息 (防1200错误 + 指数跳跃 + 动态Batch + 熔断机制)
        [修复] 不再使用 list 引用传递状态，改为返回新的 strike 计数
        Returns:
            (batch, next_cursor, success, new_strike_count)
        """
        batch_size = self.config.get("batch_size", 100)
        
        # --- 熔断检查 ---
        MAX_RETRY_STRIKE = 15 
        if current_strike > MAX_RETRY_STRIKE:
            logger.error(f"Portrayal: 连续失败次数过多 ({current_strike}次)，触发熔断停止回溯。")
            return [], 0, False, current_strike
        # ----------------

        try:
            payload = {
                "group_id": int(group_id),
                "count": batch_size,
                "reverseOrder": True
            }
            if cursor_seq > 0:
                payload["message_seq"] = cursor_seq

            res = await client.api.call_action("get_group_msg_history", **payload)
            
            if not res or not isinstance(res, dict): 
                return [], 0, False, current_strike
            
            batch = res.get("messages", [])
            if not batch: 
                # 虽然成功调用但无消息，视为到底了，不增加 strike
                return [], 0, True, 0 
            
            oldest_msg = batch[0]
            next_cursor = int(oldest_msg.get("message_seq") or oldest_msg.get("message_id") or 0)
            
            # 成功获取，重置错误计数
            return batch, next_cursor, True, 0

        except Exception as e:
            err_msg = str(e)
            # 处理 1200 错误或消息不存在的情况
            if "1200" in err_msg or "不存在" in err_msg:
                new_strike = current_strike + 1
                
                base_jump = max(50, batch_size) 
                # 限制指数最大倍数，防止溢出
                jump_step = base_jump * (2 ** (min(new_strike, 8) - 1))
                
                if new_strike <= 5 or new_strike % 5 == 0:
                    logger.warning(f"Portrayal: 游标 {cursor_seq} 处断层 (重试 {new_strike}/{MAX_RETRY_STRIKE} 次)，尝试向下跳跃 {jump_step} 条...")
                
                new_cursor = cursor_seq - jump_step
                return [], new_cursor, False, new_strike
            else:
                logger.warning(f"Portrayal: API请求中断: {e}")
                return [], 0, False, current_strike

    async def _fetch_user_history_smart(self, event: AiocqhttpMessageEvent, target_id: str, max_rounds: int):
        """[上层] 深度优先抓取：固定拉取 max_rounds 轮"""
        group_id = event.get_group_id()
        
        collected_texts = []
        cursor_seq = 0
        error_strike = 0  # [修复] 使用普通整数变量
        real_rounds = 0
        
        while real_rounds < max_rounds:
            batch, next_cursor, success, new_strike = await self._fetch_next_batch_robust(
                event.bot, group_id, cursor_seq, error_strike
            )
            error_strike = new_strike # 更新状态
            
            if not success:
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
        """尝试查找指定ID的Provider"""
        if not target_id: return None
        target_id_lower = target_id.lower()
        
        all_providers = []
        # 尝试从注册表中获取
        if hasattr(self.context, "register"):
            reg_providers = getattr(self.context.register, "providers", None)
            if isinstance(reg_providers, dict):
                all_providers.extend(reg_providers.values())
            elif isinstance(reg_providers, list):
                all_providers.extend(reg_providers)
        
        # 尝试从上下文获取
        if hasattr(self.context, "get_all_providers"):
            try:
                all_providers.extend(self.context.get_all_providers())
            except Exception: pass

        seen = set()
        for p in all_providers:
            if not p or id(p) in seen: continue
            seen.add(id(p))
            
            p_ids = []
            # 收集该 Provider 的所有可能 ID
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
        # [修复] 平台兼容性检查
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("❌ 本插件依赖 OneBot 协议获取历史消息，当前适配器不支持。")
            return

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
        # 限制最大轮数，防止滥用
        max_rounds = min(100, max(1, max_rounds))
        
        batch_size = self.config.get("batch_size", 100)
        total_raw_msgs = max_rounds * batch_size

        texts = []
        completion_text = ""

        # 缓存逻辑
        if not force_refresh and target_id in self.texts_cache:
            texts = self.texts_cache[target_id]
            # 刷新缓存位置 (LRU)
            self.texts_cache.move_to_end(target_id)
            completion_text = f"✅ 从缓存加载：找到了 {len(texts)} 条有效发言。"
        else:
            yield event.plain_result(f"🔍 正在深度回溯 {nickname} 的最近消息 (深度: {max_rounds}轮 / 约{total_raw_msgs}条)...")
            texts, rounds_done = await self._fetch_user_history_smart(event, target_id, max_rounds)
            if texts:
                # [修复] 写入缓存并清理旧数据
                self.texts_cache[target_id] = texts
                self.texts_cache.move_to_end(target_id)
                if len(self.texts_cache) > self.MAX_CACHE_SIZE:
                    self.texts_cache.popitem(last=False) # 移除最旧的
                
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
                        if hasattr(event.message_obj, "message_id"): 
                            chain.append(Reply(id=event.message_obj.message_id))
                        
                        if completion_text:
                            chain.append(Plain(completion_text + "\n"))

                        if str(img_result).startswith("http"): chain.append(Image.fromURL(img_result))
                        else: chain.append(Image.fromFileSystem(img_result))
                        
                        yield event.chain_result(chain)
                        sent_success = True
                except Exception as e:
                    logger.warning(f"Portrayal: 转图失败 {e}，回退文本")
            
            if not sent_success:
                final_msg = f"{completion_text}\n\n{result_text}" if completion_text else result_text
                yield event.plain_result(final_msg)

        except Exception as e:
            logger.error(f"画像生成失败: {e}")
            yield event.plain_result(f"❌ 分析过程中发生错误: {e}")
