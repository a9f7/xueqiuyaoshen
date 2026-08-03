#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为每条发言（原贴 / 评论 / 转发）标注 3-5 个语义标签，便于后续检索。

标签体系（多维度，按优先级从高到低）：
  1. 行业/主题   industry    —— 医药、科技、金融、地产、消费、新能源、能源、黄金有色…
  2. 地域        region      —— 美国、中国、中国香港、欧洲、日本、全球、新兴市场
  3. 视角层级    perspective —— 宏观、中观、微观
  4. 资产类别    asset       —— 股票、债券、外汇、商品、加密货币、ETF、期权
  5. 观点倾向    stance      —— 看多、看空、中性、风险提示、复盘
  6. 内容类型    ctype       —— 行情点评、政策解读、个股分析、答疑、随笔、数据

算法：规则化关键词匹配（可复现、可跑全量）。每条从各维度取命中得分最高的标签，
再按优先级拼成 3-5 个最终标签；不足 3 个时由内容类型/「其他」补足。

输出：
  - 原地给 posts.json / comments.json / reposts.json 每条加 "tags": [...]
  - data/tags_index.json  —— { tags: [{tag,count,dim}], total_tagged }
"""
import json
import os
import re
import sys
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ---------------------------------------------------------------------------
# 标签词典：维度 -> { 标签: [关键词...] }
# 顺序即维度优先级（靠前的维度优先进入最终标签）
# ---------------------------------------------------------------------------
DIMENSIONS = [
    ("industry", {
        "医药": ["药", "医药", "biotech", "生物", "临床", "fda", "fdx", "创新药", "仿制药",
                 "疫苗", "cxo", "cro", "cdmo", "医疗器械", "医院", "中药", "化学药", "肿瘤",
                 "癌症", "糖尿病", "靶点", "抗体", "基因", "细胞", "医保", "集采", "制药",
                 "辉瑞", "moderna", "modena", "礼来", "诺和诺德", "恒瑞", "药明", "迈瑞", "健友",
                 "司美格鲁肽", "glp", "减重", "医美"],
        "科技": ["科技", "半导体", "芯片", "光刻", "ai", "人工智能", "大模型", "算力", "gpu",
                 "英伟达", "微软", "谷歌", "苹果", "软件", "saas", "云", "算法", "机器人",
                 "自动驾驶", "meta", "亚马逊", "openai", "算力", "数据中心"],
        "互联网": ["互联网", "电商", "平台", "腾讯", "阿里", "字节", "美团", "抖音", "快手",
                   "流量", "用户", "变现", "广告", "订阅"],
        "金融": ["金融", "银行", "保险", "券商", "信托", "基金", "私募", "公募", "资管", "理财",
                 "投资", "财富", "投行", "招商银行", "平安", "兴业", "宁波银行", "估值"],
        "地产": ["地产", "房地产", "房价", "楼市", "房企", "万科", "恒大", "碧桂园", "物业",
                 "棚改", "限购", "按揭", "房贷", "土拍"],
        "消费": ["消费", "白酒", "食品", "饮料", "零售", "餐饮", "家电", "服装", "免税", "茅台",
                 "五粮液", "伊利", "海天", "酱油", "啤酒", "奢侈品", "免税", "商超"],
        "新能源": ["新能源", "光伏", "锂电", "电池", "储能", "风电", "碳中和", "电动车", "充电桩",
                   "宁德", "比亚迪", "整车", "氢能", "钙钛矿"],
        "汽车": ["汽车", "整车", "零部件", "燃油车", "蔚来", "理想", "小鹏", "特斯拉", "销量", "交付"],
        "能源": ["石油", "原油", "煤炭", "天然气", "油气", "中石油", "中石化", "海油", "opec",
                 "页岩", "能源", "电力", "火电", "水电"],
        "黄金有色": ["黄金", "有色", "金属", "铜", "铝", "锂矿", "稀土", "钢铁", "矿产", "白银"],
        "军工": ["军工", "国防", "导弹", "飞机", "船舶", "航天", "卫星"],
        "农业": ["农业", "农产品", "粮食", "猪肉", "养殖", "种子", "化肥", "猪周期"],
        "传媒游戏": ["传媒", "游戏", "影视", "出版", "广告", "元宇宙", "直播", "短剧"],
        "通信": ["通信", "5g", "6g", "运营商", "移动", "电信", "联通", "设备商"],
        "基建机械": ["基建", "工程机械", "建筑", "水泥", "重工", "挖掘机"],
        "教育": ["教育", "培训", "考研", "k12", "双减", "学校"],
    }),
    ("region", {
        "美国": ["美国", "美股", "美债", "美联储", "华盛顿", "纳指", "标普", "道指", "纳斯达克",
                 "纽约", "sec", "特朗普", "拜登", "美元", "美光", "辉瑞", "moderna", "特斯拉",
                 "英伟达", "谷歌", "微软", "亚马逊", "meta", "伯克希尔", "苹果", "fda", "非农",
                 "美国银行", "摩根", "高盛", "标普500", "纳斯达克"],
        "中国": ["a股", "沪深", "上证", "创业板", "科创板", "北交所", "央行", "证监会", "国务院",
                 "发改委", "财政", "人民币", "沪深300", "中证", "茅台", "宁德", "比亚迪", "招商",
                 "平安", "工行", "建行", "国内", "中国", "稳增长", "信贷", "社融"],
        "中国香港": ["港股", "香港", "恒生", "恒指", "h股", "腾讯", "美团", "小米", "友邦", "港交所"],
        "欧洲": ["欧洲", "欧盟", "德国", "法国", "英国", "英国央行", "欧央行", "伦敦", "欧股",
                 "瑞士", "欧债", "欧元", "瑞郎"],
        "日本": ["日本", "日经", "东证", "日元", "日本央行", "丰田", "软银"],
        "全球": ["全球", "世界经济", "全球市场", "g20", "imf", "世界经济", "全球宏观"],
        "新兴市场": ["新兴市场", "印度", "越南", "巴西", "东南亚", "墨西哥"],
    }),
    ("perspective", {
        "宏观": ["经济", "gdp", "通胀", "cpi", "ppi", "利率", "美联储", "央行", "降息", "加息",
                 "货币", "财政", "汇率", "就业", "衰退", "萧条", "周期", "宏观", "流动性", "m2",
                 "社融", "pmi", "景气", "总需求", "供给", "政策", "宽松", "紧缩", "市场", "大类资产"],
        "中观": ["行业", "赛道", "产业链", "供需", "产能", "景气度", "板块", "中游", "上游",
                 "下游", "竞争格局", "龙头", "渗透率"],
        "微观": ["公司", "个股", "财报", "业绩", "营收", "利润", "管理层", "估值", "pe", "pb",
                 "roe", "分红", "回购", "董事长", "ceo", "毛利率", "净利率", "订单", "产能利用率",
                 "基本面", "经营"],
    }),
    ("asset", {
        "股票": ["股票", "个股", "持股", "建仓", "清仓", "仓位", "调仓", "止盈", "止损", "套牢",
                 "抄底", "a股", "港股", "美股", "持仓"],
        "债券": ["债券", "国债", "美债", "信用债", "收益率", "ytm", "久期", "利差"],
        "外汇": ["外汇", "汇率", "美元", "人民币", "欧元", "日元", "换汇", "套息", "贬值", "升值"],
        "商品": ["商品", "原油", "黄金", "铜", "农产品", "期货", "大宗", "commodity"],
        "加密货币": ["比特币", "以太坊", "crypto", "区块链", "币圈", "web3", "usdt"],
        "ETF": ["etf", "指数基金", "宽基", "行业etf", "联接基金"],
        "期权": ["期权", "call", "put", "波动率", "iv", "行权"],
    }),
    ("stance", {
        "看多": ["看好", "看多", "牛市", "上涨", "机会", "低估", "买入", "加仓", "建仓", "抄底",
                 "乐观", "利好", "潜力", "空间", "性价比", "值得", "坚定", "底部"],
        "看空": ["看空", "熊市", "下跌", "风险", "高估", "卖出", "减仓", "清仓", "悲观", "利空",
                 "泡沫", "警惕", "回避", "谨慎", "割肉", "顶部", "见顶"],
        "中性": ["中性", "震荡", "观望", "盘整", "持平", "平稳", "结构性"],
        "风险提示": ["风险", "注意", "警惕", "小心", "警示", "不确定性", "黑天鹅", "爆雷", "违约", "踩雷"],
        "复盘": ["复盘", "回顾", "总结", "反思", "操作", "实盘", "收益", "盈亏", "账户", "记录",
                 "今年", "去年", "收益率", "年化"],
    }),
    ("ctype", {
        "行情点评": ["今日", "今天", "盘面", "行情", "涨", "跌", "指数", "收盘", "开盘", "放量",
                 "缩量", "走势", "大盘", "盘中", "周线", "日线", "市场", "交易", "资金", "波动",
                 "板块", "持仓", "仓位", "账户", "盈亏", "赚钱", "亏钱"],
        "政策解读": ["政策", "法规", "监管", "文件", "通知", "发布", "出台", "解读", "审批", "集采",
                 "国常会", "指导意见"],
        "个股分析": ["分析", "逻辑", "价值", "估值", "基本面", "财报", "业绩", "公司", "个股", "龙头",
                 "为什么", "看好", "原因"],
        "答疑": ["问", "回答", "回复", "请教", "怎么看", "如何看待", "帮忙", "解答", "？", "?"],
        "随笔": ["随笔", "想法", "感觉", "随便", "闲聊", "生活", "日常", "心情", "感慨", "碎碎念",
                 "今天", "周末", "晚上"],
        "数据": ["数据", "统计", "图表", "报告", "调研", "测算", "数", "披露", "公告"],
    }),
]

# tag -> 维度 反查
TAG_DIM = {}
for dim, d in DIMENSIONS:
    for tag in d:
        TAG_DIM[tag] = dim


def score_text(text):
    """返回 {维度: [(标签, 得分), ...] 按得分降序}"""
    if not text:
        return {}
    low = text.lower()
    res = {}
    for dim, d in DIMENSIONS:
        scored = []
        for tag, kws in d.items():
            s = 0
            for kw in kws:
                # 英文关键词大小写不敏感；中文直接计数
                if re.search(r"[a-zA-Z]", kw):
                    s += low.count(kw.lower())
                else:
                    s += text.count(kw)
            if s > 0:
                scored.append((tag, s))
        if scored:
            scored.sort(key=lambda x: x[1], reverse=True)
            res[dim] = scored
    return res


def tag_one(text):
    """给单条文本生成 3-5 个标签（按维度优先级拼接 + 补足）"""
    scored = score_text(text)
    # 维度优先级即 DIMENSIONS 顺序
    dim_order = [d[0] for d in DIMENSIONS]

    # 1) 每个维度取最高分标签作为候选（core）
    core = []
    for dim in dim_order:
        if dim in scored and scored[dim]:
            core.append(scored[dim][0][0])

    # 2) 其余候选（除 core 已取外）按 (维度优先级, 得分) 排序作为 extras
    extras = []
    for dim in dim_order:
        if dim in scored:
            for tag, s in scored[dim][1:]:  # 跳过已进 core 的最高分
                extras.append((dim, s, tag))

    def dim_prio(dim):
        return dim_order.index(dim)

    extras.sort(key=lambda x: (dim_prio(x[0]), x[1]), reverse=False)

    # 组装：core 顺序保持维度优先级；extras 按优先级补满到 5
    result = list(core)
    for dim, s, tag in extras:
        if len(result) >= 5:
            break
        if tag not in result:
            result.append(tag)

    # 3) 宁缺毋滥：只保留「真实命中」的标签（1-5 个）。
    #    仅当完全无法归类（0 维命中）时才标「其他」，避免无意义填充污染检索。
    if len(result) == 0:
        result = ["其他"]

    return result[:5]


def tag_for_item(item):
    """评论/转发会合并自身文本 + 内嵌原文，主体仍是本条发言。"""
    text = item.get("text") or item.get("description") or ""
    if item.get("kind") in ("comment", "repost"):
        orig = item.get("original") or {}
        if orig.get("text"):
            text = text + " " + orig["text"]
    return tag_one(text)


def process_file(path):
    if not os.path.exists(path):
        return 0, 0
    arr = json.load(open(path, encoding="utf-8"))
    n = 0
    for it in arr:
        it["tags"] = tag_for_item(it)
        n += 1
    json.dump(arr, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    tagged = sum(1 for it in arr if it.get("tags"))
    return n, tagged


def main():
    files = [
        DATA / "posts.json",
        DATA / "comments.json",
        DATA / "reposts.json",
    ]
    total = 0
    tag_counter = Counter()
    for f in files:
        n, tagged = process_file(f)
        total += n
        print(f"[tag] {f.name}: {n} 条, 已标注 {tagged} 条")
        # 统计标签（重新读以拿最新 tags）
        for it in json.load(open(f, encoding="utf-8")):
            for t in it.get("tags", []):
                tag_counter[t] += 1

    # 写 tags_index.json
    tags_list = [
        {"tag": t, "count": c, "dim": TAG_DIM.get(t, "other")}
        for t, c in tag_counter.most_common()
    ]
    idx = {
        "updated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_tagged": total,
        "distinct_tags": len(tags_list),
        "tags": tags_list,
    }
    json.dump(idx, open(DATA / "tags_index.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"[tag] 共 {total} 条, 生成 {len(tags_list)} 个不同标签 -> data/tags_index.json")
    print("[tag] Top 15:", ", ".join(f"{t['tag']}({t['count']})" for t in tags_list[:15]))


if __name__ == "__main__":
    main()
