from app.utils_dependency import *
from app.models import (
    Prize,
    Pool,
    PoolItem,
    PoolRecord,
    Notification,
    Organization,
)
from app.utils import get_person_or_org
from app.wechat_send import WechatApp, WechatMessageLevel
from app.notification_utils import bulk_notification_create, notification_create
from generic.models import User, YQPointRecord

from django.db.models import QuerySet, Q, Sum
from django.forms.models import model_to_dict

from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import random

__all__ = [
    'get_pools_and_items'
    'buy_exchange_item',
    'buy_lottery_pool',
    'buy_random_pool',
    'run_lottery',
]


def get_pools_and_items(pool_type: Pool.Type, user: User, frontend_dict: Dict[str, any]):
    """
    获取某一种类的所有当前开放的pool的前端所需信息

    :param pool_type: pool种类
    :type pool_type: Pool.Type
    :param user: 当前用户
    :type user: User
    :param frontend_dict: 前端字典
    :type frontend_dict: Dict[str, any]
    """
    pools = Pool.objects.filter(
        Q(type=pool_type) & Q(start__lte=datetime.now())
        & (Q(end__isnull=True) | Q(end__gte=datetime.now() - timedelta(days=1))))

    pools_info = []
    # 此列表中含有若干dict，每个dict对应一个待展示的pool，例如：
    # {
    #     "title": "xxx", "type": "兑换/抽奖/盲盒",
    #     "entry_time": 1, # 对于盲盒/抽奖奖池，一个用户最多能买几次
    #     "ticket_price": 1, # 盲盒/抽奖奖池价格
    #     "start": "2022-9-4", "end": "2022-9-5", # end可能为空
    #     "redeem_start": "2022-9-10", "redeem_end": "2022-9-20", # 指线下获取奖品实物的时间，均可能为空
    #
    #     "status": 0/1, # 0表示进行中的奖池，1表示结束一天内的抽奖奖池
    #     "items": [], # 含有若干dict，每个dict代表该奖池中的一个poolitem
    #         # key包括"id", "origin_num", "consumed_num", "exchange_price",
    #         # "exchange_limit", "is_big_prize", "is_empty",
    #         # "prize__name", "prize__more_info", "prize__stock",
    #         # "prize__reference_price", "prize__image", "prize__id",
    #         # 以及origin_num-consumed_num得到的remain_num
    #         # 如果是兑换类奖池，还有my_exchange_time，即当前用户兑换过该item多少次
    #     "my_entry_time": 0, # 当前用户进过抽奖/盲盒奖池多少次
    #     "records_num": 0, # 抽奖/盲盒奖池总共被买了多少次
    #     "capacity": 0, # 盲盒奖池最多能被买多少次（即包括谢谢参与在内的所有poolitem的数量和）
    #     "results": { # 已结束的抽奖奖池有这一项，表示抽奖结果，
    #         # 其中包含"big_prize_results"和"normal_prize_results"两个列表
    #         # 每个列表中又是若干词典，每个词典表示一种奖品的获奖情况（这些词典按奖品参考价格的降序排列），
    #         # 其key包括prize_name、prize_image和winners，其中winners是NaturalPerson.name的list，即这种奖品的获奖者列表
    #         "big_prize_results": [
    #             {"prize_name": "大奖1", "prize_image": "imageurl", "winners": ["张三", "李四"]},
    #             {"prize_name": "大奖2", "prize_image": "imageurl", "winners": ["Alice"]},
    #         ],
    #         "normal_prize_results": [
    #             {"prize_name": "奖品1", "prize_image": "imageurl", "winners": ["王五"]},
    #             {"prize_name": "奖品2", "prize_image": "imageurl", "winners": ["Alice", "Bob"]},
    #         ]
    #     }
    # }

    for pool in pools:
        this_pool_info = model_to_dict(pool)
        if pool.start <= datetime.now() and (pool.end is None or pool.end >= datetime.now()):
            this_pool_info["status"] = 0
        else:
            this_pool_info["status"] = 1

        this_pool_info["capacity"] = pool.get_capacity()
        this_pool_items = list(pool.items.filter(prize__isnull=False).values(
            "id", "origin_num", "consumed_num", "exchange_price",
            "exchange_limit", "is_big_prize",
            "prize__name", "prize__more_info", "prize__stock",
            "prize__reference_price", "prize__image", "prize__id"
        ))
        for item in this_pool_items:
            item["remain_num"] = item["origin_num"] - item["consumed_num"]
        this_pool_info["items"] = sorted(
            this_pool_items, key=lambda x: -x["remain_num"]) # 按剩余数量降序排序，已卖完的在最后

        if pool_type != Pool.Type.EXCHANGE:
            this_pool_info["my_entry_time"] = PoolRecord.objects.filter(
                user=user, pool=pool).count()
            this_pool_info["records_num"] = PoolRecord.objects.filter(
                pool=pool).count()
            if pool_type == Pool.Type.RANDOM:
                for item in this_pool_items:
                    # 此处显示的是抽奖概率，目前使用原始的占比
                    percent = (100 * item["origin_num"] / this_pool_info["capacity"])
                    if percent == int(percent):
                        percent = int(percent)
                    elif round(percent, 1) != 0:
                        # 保留最低精度
                        percent = round(percent, 1)
                    item["probability"] = percent
            # LOTTERY类的pool不需要capacity
        else:
            for item in this_pool_items:
                item["my_exchange_time"] = PoolRecord.objects.filter(
                    user=user, pool=pool, prize=item["prize__id"]).count()
            # EXCHANGE类的pool不需要capcity和records_num和my_entry_time

        if this_pool_info["status"] == 1: # 如果是刚结束的抽奖，需要填充results
            big_prize_items = PoolItem.objects.filter(
                pool=pool, is_big_prize=True).order_by("-prize__reference_price")
            normal_prize_items = PoolItem.objects.filter(
                pool=pool, is_big_prize=False).order_by("-prize__reference_price")
            big_prizes_and_winners = []
            normal_prizes_and_winners = []

            for big_prize_item in big_prize_items:
                big_prizes_and_winners.append(
                    {"prize_name": big_prize_item.prize.name, "prize_image": big_prize_item.prize.image})
                winner_names = list(PoolRecord.objects.filter(
                    pool=pool, prize=big_prize_item.prize).values_list(
                        "user__name", flat=True))  # TODO: 需要distinct()吗？
                big_prizes_and_winners[-1]["winners"] = winner_names
            for normal_prize_item in normal_prize_items:
                normal_prizes_and_winners.append(
                    {"prize_name": normal_prize_item.prize.name, "prize_image": normal_prize_item.prize.image})
                winner_names = list(PoolRecord.objects.filter(
                    pool=pool, prize=normal_prize_item.prize).values_list(
                        "user__name", flat=True))  # TODO: 需要distinct()吗？
                normal_prizes_and_winners[-1]["winners"] = winner_names
            this_pool_info["results"] = {}
            this_pool_info["results"]["big_prize_results"] = big_prizes_and_winners
            this_pool_info["results"]["normal_prize_results"] = normal_prizes_and_winners

        pools_info.append(this_pool_info)

    frontend_dict["pools_info"] = pools_info


def buy_exchange_item(user: User, poolitem_id: str) -> MESSAGECONTEXT:
    """
    购买兑换奖池的某个奖品

    :param user: 当前用户
    :type user: User
    :param poolitem_id: 待购买的奖池奖品id，因为是前端传过来的所以是str
    :type poolitem_id: str
    :return: 表明购买结果的warn_code和warn_message
    :rtype: MESSAGECONTEXT
    """
    # 检查奖品是否可以购买
    try:
        poolitem_id = int(poolitem_id)
        poolitem = PoolItem.objects.get(
            id=poolitem_id, pool__type=Pool.Type.EXCHANGE)
    except:
        return wrong('奖品不存在!')
    if poolitem.pool.start > datetime.now():
        return wrong('兑换时间未开始!')
    if poolitem.pool.end is not None and poolitem.pool.end < datetime.now():
        return wrong('兑换时间已结束!')
    if poolitem.origin_num - poolitem.consumed_num <= 0:
        return wrong('奖品已售罄!')

    my_exchanged_time = PoolRecord.objects.filter(
        user=user, pool=poolitem.pool, prize=poolitem.prize).count()
    if my_exchanged_time >= poolitem.exchange_limit:
        return wrong('您兑换该奖品的次数已达上限!')
    
    try:
        with transaction.atomic():
            poolitem = PoolItem.objects.select_for_update().get(
                id=poolitem_id, pool__type=Pool.Type.EXCHANGE)
            assert poolitem.pool.start <= datetime.now(), "兑换时间未开始!"
            assert poolitem.pool.end is None or poolitem.pool.end >= datetime.now(), "兑换时间已结束!"
            assert poolitem.origin_num - poolitem.consumed_num > 0, "奖品已售罄!"
            my_exchanged_time = PoolRecord.objects.filter(
                user=user, pool=poolitem.pool, prize=poolitem.prize).count()
            assert my_exchanged_time < poolitem.exchange_limit, '您兑换该奖品的次数已达上限!'
            assert user.YQpoint >= poolitem.exchange_price, '您的元气值不足，兑换失败!'

            # 更新奖品状态
            poolitem.consumed_num += 1
            poolitem.save()

            # 创建兑换记录
            PoolRecord.objects.create(
                user=user,
                pool=poolitem.pool,
                prize=poolitem.prize,
                status=PoolRecord.Status.UN_REDEEM,
            )

            # 扣除元气值
            User.objects.modify_YQPoint(
                user,
                -poolitem.exchange_price,
                source=f'兑换奖池：{poolitem.pool.title}-{poolitem.prize.name}',
                source_type=YQPointRecord.SourceType.CONSUMPTION
            )
    except AssertionError as e:
        return wrong(str(e))      

    return succeed('兑换成功!')


def buy_lottery_pool(user: User, pool_id: str) -> MESSAGECONTEXT:
    """
    购买抽奖奖池

    :param user: 当前用户
    :type user: User
    :param pool_id: 待购买的奖池id，因为是前端传过来的所以是str
    :type pool_id: str
    :return: 表明购买结果的warn_code和warn_message
    :rtype: MESSAGECONTEXT
    """
    # 检查抽奖奖池状态
    try:
        pool_id = int(pool_id)
        pool = Pool.objects.get(id=pool_id, type=Pool.Type.LOTTERY)
    except:
        return wrong('抽奖不存在!')
    if pool.start > datetime.now():
        return wrong('抽奖未开始!')
    if pool.end is not None and pool.end < datetime.now():  # 实际上抽奖类的奖池的end应该不可能是None
        return wrong('抽奖已结束!')
    my_entry_time = PoolRecord.objects.filter(pool=pool, user=user).count()
    if my_entry_time >= pool.entry_time:
        return wrong('您在本奖池中抽奖的次数已达上限!')
    
    try:
        with transaction.atomic():
            pool = Pool.objects.select_for_update().get(id=pool_id, type=Pool.Type.LOTTERY)
            assert pool.start <= datetime.now(), '抽奖未开始!'
            assert pool.end is None or pool.end >= datetime.now(), '抽奖已结束!'
            my_entry_time = PoolRecord.objects.filter(pool=pool, user=user).count()
            assert my_entry_time < pool.entry_time, '您在本奖池中抽奖的次数已达上限!'
            assert user.YQpoint >= pool.ticket_price, '您的元气值不足，兑换失败!'

            # 创建抽奖记录
            PoolRecord.objects.create(
                user=user,
                pool=pool,
                status=PoolRecord.Status.LOTTERING,
            )

            # 扣除元气值
            User.objects.modify_YQPoint(
                user,
                -pool.ticket_price,
                source=f'抽奖奖池：{pool.title}',
                source_type=YQPointRecord.SourceType.CONSUMPTION
            )
    except AssertionError as e:
        return wrong(str(e))      
    
    return succeed('成功进行一次抽奖!您可以在抽奖时间结束后查看抽奖结果~')


def select_random_prize(poolitems: QuerySet[PoolItem], select_num: Optional[int] = None) -> List[int]:
    """
    实现无放回随机抽取select_num个PoolItem（的id）,初始时每种PoolItem有origin_num-consumed_num个

    :param poolitems: 待抽取的PoolItem构成的QuerySet（每个元素表示一种PoolItem而非一个）
    :type poolitems: QuerySet[PoolItem]
    :param select_num: 抽几个，若为None则抽取所有奖品，也即对poolitems做一次shuffle, defaults to None
    :type select_num: Optional[int], optional
    :return: 抽出的poolitem的id组成的list，长度等于select_num
    :rtype: List[int]
    """
    assert poolitems.count() > 0

    num_all_items = 0  # 奖品的总数
    item_dict = {}  # int: PoolItem，实现把一个自然数区间映射到一种奖品
    for item in poolitems:
        if item.origin_num - item.consumed_num <= 0:
            continue
        item_dict[num_all_items] = item
        num_all_items += item.origin_num - item.consumed_num

    if select_num is None:  # 不给出select_num就默认抽取所有奖品，也即对poolitems做一次shuffle
        select_num = num_all_items
    assert select_num <= num_all_items

    selected_idx = random.sample(
        range(num_all_items), select_num)  # 选出select_num个序号
    selected_items_id = []
    for idx in selected_idx:
        for key in sorted(item_dict.keys(), reverse=True):
            if idx >= key:  # 寻找idx落入的区间
                selected_items_id.append(
                    item_dict[key].id)  # 把idx映射为PoolItem.id
                break
    return selected_items_id


def buy_random_pool(user: User, pool_id: str) -> Tuple[MESSAGECONTEXT, int, int]:
    """
    购买盲盒

    :param user: 当前用户
    :type user: User
    :param pool_id: 待购买的奖池id，因为是前端传过来的所以是str
    :type pool_id: str
    :return: 表明购买结果的warn_code和warn_message；买到的prize的id（如果购买失败就是-1）；
                表明盲盒结果的一个int：2表示无反应、1表示开出空盒、0表示开出奖品
    :rtype: Tuple[MESSAGECONTEXT, int, int]
    """
    # 检查盲盒奖池状态
    try:
        pool_id = int(pool_id)
        pool = Pool.objects.get(id=pool_id, type=Pool.Type.RANDOM)
    except:
        return wrong('盲盒不存在!'), -1, 2
    if pool.start > datetime.now():
        return wrong('盲盒兑换时间未开始!'), -1, 2
    if pool.end is not None and pool.end < datetime.now():
        return wrong('盲盒兑换时间已结束!'), -1, 2
    my_entry_time = PoolRecord.objects.filter(pool=pool, user=user).count()
    if my_entry_time >= pool.entry_time:
        return wrong('您兑换这款盲盒的次数已达上限!'), -1, 2
    total_entry_time = PoolRecord.objects.filter(pool=pool).count()
    capacity = pool.get_capacity()
    if capacity <= total_entry_time:
        return wrong('盲盒已售罄!'), -1, 2
    
    try:
        with transaction.atomic():
            pool = Pool.objects.select_for_update().get(id=pool_id, type=Pool.Type.RANDOM)
            assert pool.start <= datetime.now(), '盲盒兑换时间未开始!'
            assert pool.end is None or pool.end >= datetime.now(), '盲盒兑换时间已结束!'
            my_entry_time = PoolRecord.objects.filter(pool=pool, user=user).count()
            assert my_entry_time < pool.entry_time, '您兑换这款盲盒的次数已达上限!'
            assert user.YQpoint >= pool.ticket_price, '您的元气值不足，兑换失败!'
            total_entry_time = PoolRecord.objects.filter(pool=pool).count()
            capacity = pool.get_capacity()
            assert capacity > total_entry_time, '盲盒已售罄!'

            # 开盒，修改poolitem记录，创建poolrecord记录
            items = pool.items.select_for_update().all()
            real_item_id = select_random_prize(items, 1)[0]
            modify_item: PoolItem = PoolItem.objects.select_for_update().get(id=real_item_id)
            modify_item.consumed_num += 1
            modify_item.save()

            if modify_item.is_empty: # 如果是空盲盒，没法兑奖，record的状态记为NOT_LUCKY
                item_status = PoolRecord.Status.NOT_LUCKY
            else:
                item_status = PoolRecord.Status.UN_REDEEM
            PoolRecord.objects.create(
                user=user,
                pool=pool,
                status=item_status,
                prize=modify_item.prize,
            )

            # 扣除元气值
            User.objects.modify_YQPoint(
                user,
                -pool.ticket_price,
                source=f'盲盒奖池：{pool.title}',
                source_type=YQPointRecord.SourceType.CONSUMPTION
            )
            if modify_item.prize is None:
                return succeed('兑换盲盒成功!'), -1, 1
            return succeed('兑换盲盒成功!'), modify_item.prize.id, int(modify_item.is_empty)
    except AssertionError as e:
        return wrong(str(e)), -1, 2


def run_lottery(pool_id: int):
    """
    抽奖；更新PoolRecord表和PoolItem表；给所有参与者发送通知

    :param pool_id: 待抽取的抽奖奖池id
    :type pool_id: int
    """
    # 部分参考了course_utils.py的draw_lots函数
    pool = Pool.objects.get(id=pool_id, type=Pool.Type.LOTTERY)
    assert not PoolRecord.objects.filter(  # 此时pool关联的所有records都应该是LOTTERING
        pool=pool).exclude(status=PoolRecord.Status.LOTTERING).exists()
    with transaction.atomic():
        related_records = PoolRecord.objects.filter(
            pool=pool, status=PoolRecord.Status.LOTTERING)
        records_num = related_records.count()
        if records_num == 0:
            return

        # 抽奖
        record_ids_and_participant_ids = list(
            related_records.values("id", "user__id"))
        items = pool.items.all()
        user2prize_names = {d["user__id"]: []
                            for d in record_ids_and_participant_ids}  # 便于发通知
        winner_record_id2item_id = {}  # poolrecord.id: poolitem.id，便于更新poolrecord
        loser_record_ids = []  # poolrecord.id，便于更新poolrecord
        num_all_items = 0  # 该奖池中奖品总数
        for item in items:
            num_all_items += item.origin_num - item.consumed_num
        if num_all_items >= records_num:  # 抽奖记录数少于或等于奖品数，人人有奖，给每个记录分配一个随机奖品
            shuffled_items = select_random_prize(
                items, records_num)  # 随机选出待发放的奖品
            for i in range(records_num):  # 遍历所有记录，每个记录都有奖品
                user2prize_names[record_ids_and_participant_ids[i]["user__id"]].append(
                    items.get(id=shuffled_items[i]).prize.name
                )
                winner_record_id2item_id[record_ids_and_participant_ids[i]
                                         ["id"]] = shuffled_items[i]
        else:  # 抽奖记录数多于奖品数，给每个奖品分配一个中奖者
            for item in items:  # 遍历所有奖品，每个奖品都会送给一个记录
                for i in range(item.origin_num - item.consumed_num):
                    winner_record_index = random.randint(
                        0, len(record_ids_and_participant_ids) - 1)
                    user2prize_names[record_ids_and_participant_ids[winner_record_index]["user__id"]].append(
                        item.prize.name)
                    winner_record_id2item_id[record_ids_and_participant_ids[winner_record_index]["id"]] = item.id
                    # 因为记录多，奖品少，这里肯定不会pop成空列表
                    record_ids_and_participant_ids.pop(winner_record_index)
            # pop剩下的就是没中奖的那些记录
            loser_record_ids = [d["id"]
                                for d in record_ids_and_participant_ids]

        # 更新数据库
        for winner_record_id, poolitem_id in winner_record_id2item_id.items():
            record = PoolRecord.objects.select_for_update().get(id=winner_record_id)
            item = PoolItem.objects.select_for_update().get(id=poolitem_id)
            record.status = PoolRecord.Status.UN_REDEEM
            record.prize = item.prize
            record.time = datetime.now()
            item.consumed_num += 1
            record.save()
            item.save()
        for loser_record_id in loser_record_ids:
            record = PoolRecord.objects.select_for_update().get(id=loser_record_id)
            record.status = PoolRecord.Status.NOT_LUCKY
            record.time = datetime.now()
            record.save()

        # 给中奖的同学发送通知
        sender = Organization.objects.get(oname=YQP_ONAME).get_user()
        for user_id in user2prize_names.keys():
            receiver = User.objects.get(id=user_id)
            typename = Notification.Type.NEEDREAD
            title = Notification.Title.LOTTERY_INFORM
            content = f"恭喜您在奖池【{pool.title}】中抽中奖品"
            for prize_name in user2prize_names[user_id]:
                content += f"【{prize_name}】"  # 可能出现重复，即一种奖品中了好几次，不过感觉问题也不太大
            notification_create(
                receiver=receiver,
                sender=sender,
                typename=typename,
                title=title,
                content=content,
                # URL=f'', # TODO: 我的奖品页面？
                publish_to_wechat=True,
                publish_kws={
                    "app": WechatApp.TO_PARTICIPANT,
                    "level": WechatMessageLevel.IMPORTANT,
                },
            )

        # 给没中奖的同学发送通知
        receivers = PoolRecord.objects.filter(
            id__in=loser_record_ids,
        ).values_list("user", flat=True)
        receivers = User.objects.filter(id__in=receivers)
        content = f"很抱歉通知您，您在奖池【{pool.title}】中没有中奖"

        if len(receivers) > 0:
            bulk_notification_create(
                receivers=receivers,
                sender=sender,
                typename=typename,
                title=title,
                content=content,
                # URL=f'', # TODO: 我的奖品页面？
                publish_to_wechat=True,
                publish_kws={
                    "app": WechatApp.TO_PARTICIPANT,
                    "level": WechatMessageLevel.IMPORTANT,
                },
            )
