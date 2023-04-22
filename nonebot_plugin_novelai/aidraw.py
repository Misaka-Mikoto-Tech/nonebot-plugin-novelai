import hashlib
import re
import time
from argparse import Namespace
from asyncio import get_running_loop
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiofiles
import aiohttp
from aiohttp.client_exceptions import ClientConnectorError, ClientOSError
from nonebot import get_bot
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment, Message
from nonebot.exception import ParserExit
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import ArgumentParser

from .backend import Draw
from .config import config
from .plugins.anlas import anlas_check, anlas_set
from .plugins.daylimit import DayLimit
from .utils import BASE_TAG, CHINESE_COMMAND, HTAGS, LOW_QUALITY, parse_args, sendtosuperuser,C, text_to_img
from .utils.translation import translate
from .version import version

cd = {}
gennerating = False
wait_list = deque([])

aidraw_parser = ArgumentParser()
aidraw_parser.add_argument("tags", nargs="*", help="标签")
aidraw_parser.add_argument("-r", "--resolution", "-形状", help="画布形状/分辨率", dest="shape")
aidraw_parser.add_argument(
    "-c", "--scale", "-服从", type=float, help="对输入的服从度", dest="scale"
)
aidraw_parser.add_argument("-s", "--seed", "-种子", type=int, help="种子", dest="seed")
aidraw_parser.add_argument(
    "-b", "--batch", "-数量", type=int, default=1, help="生成数量", dest="batch"
)
aidraw_parser.add_argument("-t", "--steps", "-步数", type=int, help="步数", dest="steps")
aidraw_parser.add_argument(
    "-u", "--ntags", "-排除", default=" ", nargs="*", help="负面标签", dest="ntags"
)
aidraw_parser.add_argument(
    "-e", "--strength", "-强度", type=float, help="修改强度", dest="strength"
)
aidraw_parser.add_argument(
    "-n", "--noise", "-噪声", type=float, help="修改噪声", dest="noise"
)
aidraw_parser.add_argument(
    "-p", "--sampler", "-采样器", type=str, help="修改采样器", dest="sampler"
)
aidraw_parser.add_argument(
    "-o", "--override", "-不优化", action="store_true", help="不使用内置优化参数", dest="override"
)
aidraw_parser.add_argument(
    "-m", "--model", "-模型", type=str, help="使用模型", dest="model"
)

msg_sent_set:Set[str] = set() # bot 自己发送的消息

"""消息发送钩子，用于记录自己发送的消息"""
@Bot.on_called_api
async def handle_group_message_sent(bot: Bot, exception: Optional[Exception], api: str, data: Dict[str, Any], result: Any):
    global msg_sent_set
    if result and (api in ['send_msg', 'send_group_msg', 'send_private_msg']):
        msg_id = result.get('message_id', None)
        if msg_id:
            msg_sent_set.add(f"{bot.self_id}_{msg_id}")

aidraw_matcher = C.command(
    "",
    aliases=CHINESE_COMMAND,
)

@aidraw_matcher.handle()
async def aidraw_get(
    bot: Bot, event: GroupMessageEvent, command_arg: Message = CommandArg()
):
    global msg_sent_set
    if event.post_type == 'message_sent': # 通过bot.send发送的消息不处理
        msg_key = f"{bot.self_id}_{event.message_id}"
        if msg_key in msg_sent_set:
            msg_sent_set.remove(msg_key)
            return
        
    if len(msg_sent_set) > 10:
        msg_sent_set.clear()

    user_id = str(event.user_id)
    group_id = str(event.group_id)

    args, err_msg = parse_args(command_arg.extract_plain_text(), aidraw_parser)

    if not args:
        logger.error(f'解析指令失败:{err_msg}')
        await aidraw_matcher.finish(f"命令解析出错了!(提示词包含连字符或参数包含空格时需要用引号括起来哦)")

    if len(args.tags) == 0:
        help_msg = MessageSegment.image(await get_help_image())
        help_msg += "在线文档: https://docs.qq.com/doc/DSE5YUG9wRmJUUVdp"
        await aidraw_matcher.finish(help_msg)

    # 判断是否禁用，若没禁用，进入处理流程
    if await config.get_value(group_id, "on"):
        message = ""
        # 判断最大生成数量
        if args.batch > config.novelai_max:
            message = message + f",批量生成数量过多，自动修改为{config.novelai_max}"
            args.batch = config.novelai_max
        # 判断次数限制
        if config.novelai_daylimit and not await SUPERUSER(bot, event):
            left = DayLimit.count(user_id, args.batch)
            if left == -1:
                await aidraw_matcher.finish(f"今天你的次数不够了哦")
            else:
                message = message + f"，今天你还能够生成{left}张"
        # 判断cd
        if not SUPERUSER(bot, event):
            nowtime = time.time()
            deltatime = nowtime - cd.get(user_id, 0)
            cd_ = int(await config.get_value(group_id, "cd"))
            if deltatime < cd_:
                await aidraw_matcher.finish(
                    f"你冲的太快啦，请休息一下吧，剩余CD为{cd_ - int(deltatime)}s"
                )
            else:
                cd[user_id] = nowtime
        # 初始化参数
        args.tags = await prepocess_tags(args.tags)
        args.ntags = await prepocess_tags(args.ntags)
        logger.info(f'收到绘制请求:user:{user_id}, group:{group_id}, args:{vars(args)}')
        aidraw = Draw(user_id=user_id, group_id=group_id, **vars(args))
        # logger.info(f'aidraw.user_id:{aidraw.user_id}, aidraw.group_id:{aidraw.group_id}')
        # 检测是否有18+词条
        if not config.novelai_h:
            pattern = re.compile(f"(\s|,|^)({HTAGS})(\s|,|$)")
            if re.search(pattern, aidraw.tags) is not None:
                await aidraw_matcher.finish(f"H是不行的!")
        
        aidraw.tags_user = aidraw.tags.strip()
        aidraw.ntags_user = aidraw.ntags.strip()
        if aidraw.tags_user and aidraw.tags_user[-1]==',':
            aidraw.tags_user = aidraw.tags_user[:-1]
        if aidraw.ntags_user and aidraw.ntags_user[-1]==',':
            aidraw.ntags_user = aidraw.ntags_user[:-1]
        
        if not args.override:
            aidraw.tags = (
                BASE_TAG + await config.get_value(group_id, "tags") + "," + aidraw.tags
            )
            aidraw.ntags = LOW_QUALITY + aidraw.ntags

        # 以图生图预处理
        img_url = ""
        reply = event.reply
        if reply:
            for seg in reply.message["image"]:
                img_url = seg.data["url"]
        for seg in event.message["image"]:
            img_url = seg.data["url"]
        if img_url:
            if config.novelai_paid:
                async with aiohttp.ClientSession() as session:
                    logger.info(f"检测到图片，自动切换到以图生图，正在获取图片")
                    async with session.get(img_url) as resp:
                        aidraw.add_image(await resp.read())
                    message = f"，已切换至以图生图" + message
            else:
                await aidraw_matcher.finish(f"以图生图功能已禁用")
        logger.debug(aidraw)
        # 初始化队列
        if aidraw.cost > 0:
            anlascost = aidraw.cost
            hasanlas = await anlas_check(aidraw.user_id)
            if hasanlas >= anlascost:
                await wait_fifo(
                    bot, aidraw, anlascost, hasanlas - anlascost, message=message
                )
            else:
                await aidraw_matcher.finish(f"你的点数不足，你的剩余点数为{hasanlas}")
        else:
            await wait_fifo(bot, aidraw, message=message)
    else:
        aidraw_matcher.finish(f"novelai插件未开启")


async def wait_fifo(bot: Bot, aidraw, anlascost=None, anlas=None, message=""):
    # 创建队列
    list_len = wait_len()
    has_wait = f"排队中，你的前面还有{list_len}人" + message
    no_wait = "请稍等，图片生成中" + message
    if anlas:
        has_wait += f"\n本次生成消耗点数{anlascost},你的剩余点数为{anlas}"
        no_wait += f"\n本次生成消耗点数{anlascost},你的剩余点数为{anlas}"
    if config.novelai_limit:
        await aidraw_matcher.send(has_wait if list_len > 0 else no_wait)
        wait_list.append(aidraw)
        await fifo_gennerate(bot)
    else:
        await aidraw_matcher.send(no_wait)
        await fifo_gennerate(bot, aidraw)


def wait_len():
    # 获取剩余队列长度
    list_len = len(wait_list)
    if gennerating:
        list_len += 1
    return list_len


async def fifo_gennerate(bot: Bot, aidraw: Draw = None):
    # 队列处理
    global gennerating

    async def generate(bot: Bot, aidraw: Draw):
        id = aidraw.user_id if config.novelai_antireport else bot.self_id
        logger.info(f'generate: group_id:{aidraw.group_id}, user_id:{aidraw.user_id}')
        resp = await bot.get_group_member_info(
            group_id=aidraw.group_id, user_id=aidraw.user_id
        )
        nickname: str = (
            (resp["card"] or resp["nickname"])
            if config.novelai_antireport
            else (
                get_bot().config.nickname.pop()
                if get_bot().config.nickname
                else "nonebot-plugin-novelai"
            )
        )

        # 开始生成
        # logger.info(f"队列剩余{wait_len()}人 | 开始生成：{aidraw}")
        try:
            im = await _run_gennerate(aidraw)
        except Exception as e:
            logger.exception("生成失败")
            message = f"生成失败，"
            for i in e.args:
                message += str(i)
            await bot.send_group_msg(message=message, group_id=aidraw.group_id)
        else:
            # logger.info(f"队列剩余{wait_len()}人 | 生成完毕：{aidraw}")
            if config.novelai_pure:
                message = MessageSegment.at(aidraw.user_id)
                idx = 0

                model = aidraw.model.split('.')[0] if aidraw.model else 'None'
                ntags = f'-ntags {aidraw.ntags_user}' if aidraw.ntags_user.strip() else ''
                prompt_txt = f'绘画 {aidraw.tags_user} {ntags} \n'
                prompt_txt += f'-p "{aidraw.sampler}" -c {aidraw.scale} -t {aidraw.steps} '
                if aidraw.img2img:
                    prompt_txt += f'-e {aidraw.strength} -n {aidraw.noise} '
                prompt_txt += f'\n-r {aidraw.width}x{aidraw.height} -m {model}'
                
                img_msg= MessageSegment.image(await text_to_img(prompt_txt))
                for img in im["image"]:
                    img_msg += f'-s {aidraw.seed[idx]}\n'
                    img_msg += img
                    img_msg += '\n'
                    idx += 1
                message += img_msg
                message_data = await bot.send_group_msg(
                    message=message,
                    group_id=aidraw.group_id,
                )
            else:
                message = []
                for i in im:
                    message.append(MessageSegment.node_custom(id, nickname, i))
                message_data = await bot.send_group_forward_msg(
                    messages=message,
                    group_id=aidraw.group_id,
                )
            revoke = await config.get_value(aidraw.group_id, "revoke")
            if revoke:
                message_id = message_data["message_id"]
                loop = get_running_loop()
                loop.call_later(
                    revoke,
                    lambda: loop.create_task(bot.delete_msg(message_id=message_id)),
                )

    if aidraw:
        await generate(bot, aidraw)

    if not gennerating:
        # logger.info(f"队列开始,长度{len(wait_list)}")
        gennerating = True

        while len(wait_list) > 0:
            aidraw = wait_list.popleft()
            try:
                await generate(bot, aidraw)
            except:
                pass

        gennerating = False
        # logger.info("队列结束")
        # await version.check_update()


async def _run_gennerate(aidraw: Draw):
    # 处理单个请求
    try:
        await aidraw.run()
    except ClientConnectorError:
        await sendtosuperuser(f"远程服务器拒绝连接，请检查配置是否正确，服务器是否已经启动")
        raise RuntimeError(f"远程服务器拒绝连接，请检查配置是否正确，服务器是否已经启动")
    except ClientOSError:
        await sendtosuperuser(f"远程服务器崩掉了欸……")
        raise RuntimeError(f"服务器崩掉了欸……请等待主人修复吧")
    # 若启用ai检定，取消注释下行代码，并将构造消息体部分注释
    # message = await check_safe_method(aidraw, img_bytes, message)
    # 构造消息体并保存图片
    message = f"{config.novelai_mode}绘画完成~"
    for i in aidraw.result:
        await save_img(aidraw, i, aidraw.group_id)
        message += MessageSegment.image(i)
    for i in aidraw.format():
        message += MessageSegment.text(i)
    # 扣除点数
    if aidraw.cost > 0:
        await anlas_set(aidraw.user_id, -aidraw.cost)
    return message


emoji = re.compile(
    "["
    "\U0001F300-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\u2600-\u2B55"
    "\U00010000-\U0010ffff]+"
)


async def prepocess_tags(tags: List[str]):
    # tags: str = "".join([i for i in tags if isinstance(i, str)]).lower().replace("，",',').replace("“",'"').replace("”",'"')
    tags = [re.sub(emoji, "", tag) for tag in tags] 
    # 去除CQ码
    # tags = re.sub("\[CQ[^\s]*?]", "", tags)
    # 检测中文
    taglist = tags #tags.split(",")
    tagzh = ""
    tags_ = ""
    for tag in taglist:
        if re.search("[\u4e00-\u9fa5]", tag):
            tagzh += f"{tag},"
        else:
            tags_ += f"{tag},"
    if tagzh:
        tags_en = await translate(tagzh, "en")
        if tags_en == tagzh:
            return ""
        else:
            tags_ += tags_en.strip()
    return tags_


async def save_img(request, img_bytes: bytes, extra: str = ""):
    # 存储图片
    path = Path("data/novelai/output").resolve()
    if config.novelai_save:
        if extra:
            path_ = path / extra
        path_.mkdir(parents=True, exist_ok=True)
        hash = hashlib.md5(img_bytes).hexdigest()
        file = (path_ / hash).resolve()
        async with aiofiles.open(str(file) + ".jpg", "wb") as f:
            await f.write(img_bytes)
        if config.novelai_debug:
            async with aiofiles.open(str(file) + ".txt", "w") as f:
                await f.write(repr(request))

async def get_help_image() -> bytes:
    """生成帮助文本图片"""
    path = Path("data/novelai/help_text.txt")
    text=""
    if path.exists():
        async with aiofiles.open(path,"r", encoding="utf-8", errors="ignore") as f:
            lines = await f.readlines()
            text = "".join(lines)
    img = await text_to_img(text)
    return img