"""Controlled city names used by the China-focused city analysis."""

from __future__ import annotations

# City-named administrative divisions from the 2023 National Bureau of
# Statistics division-code release (through 2023-06-30), plus Hong Kong,
# Macao and the commonly displayed cities in Taiwan. Names omit the generic
# trailing "市", except for 芒市 where that character is part of the name.
CHINA_CITY_CATALOG_VERSION = "nbs-2023-city-names-v1"
_CITY_NAMES = """
七台河 万宁 万源 三亚 三明 三沙 三河 三门峡 上海 上饶 东兴 东台 东宁 东方 东港 东莞 东营 东阳 个旧
中卫 中山 丰城 丰镇 临夏 临江 临汾 临沂 临沧 临海 临清 临湘 丹东 丹江口 丹阳 丽水 丽江 义乌 义马
乌兰察布 乌兰浩特 乌海 乌苏 乌鲁木齐 乐山 乐平 乐昌 乐清 乐陵 九江 乳山 二连浩特 云浮 五大连池
五家渠
五常 五指山 井冈山 京山 亳州 什邡 仁怀 介休 仙桃 仪征 任丘 伊宁 伊春 会理 余姚 佛山 佳木斯 侯马 保定
保山 信宜 信阳 儋州 克拉玛依 公主岭 六安 六盘水 兰州 兰溪 共青城 兴义 兴仁 兴化 兴城 兴宁 兴平 内江
冷水江 凌海 凌源 凤城 凭祥 凯里 利川 包头 化州 北京 北安 北屯 北流 北海 北票 北镇 十堰 华亭 华蓥
华阴
南京 南充 南宁 南安 南宫 南平 南昌 南通 南阳 南雄 博乐 卫辉 原平 厦门 双河 双辽 双鸭山 古交 句容
可克达拉 台山 台州 合作 合山 合肥 吉安 吉林 吉首 同仁 同江 吐鲁番 吕梁 启东 吴川 吴忠 周口 呼伦贝尔
呼和浩特 和田 和龙 咸宁 咸阳 哈密 哈尔滨 唐山 商丘 商洛 喀什 嘉兴 嘉峪关 四会 四平 固原 图们
图木舒克
塔城 大冶 大同 大安 大庆 大理 大石桥 大连 天水 天津 天长 天门 太仓 太原 奎屯 如皋 威海 娄底 嫩江
子长 孝义 孝感 孟州 宁乡 宁国 宁安 宁德 宁波 安丘 安国 安宁 安庆 安康 安达 安阳 安陆 安顺 定州 定西
宜兴 宜城 宜宾 宜昌 宜春 宜都 宝鸡 宣城 宣威 宿州 宿迁 密山 富锦 寿光 射洪 尚志 山南 岑溪 岳阳
峨眉山 崇州 崇左 嵊州 巢湖 巩义 巴中 巴彦淖尔 常宁 常州 常德 常熟 平凉 平度 平果 平泉 平湖 平顶山
广元 广安 广州 广德 广水 广汉 庄河 庆阳 庐山 库尔勒 库车 应城 康定 廉江 廊坊 延吉 延安 建德 建瓯
开原 开封 开平 开远 张家口 张家港 张家界 张掖 弥勒 当阳 彬州 彭州 徐州 德令哈 德兴 德州 德惠 德阳
忻州 怀仁 怀化 恩平 恩施 惠州 慈溪 成都 扎兰屯 扬中 扬州 扶余 承德 抚州 抚远 抚顺 拉萨 招远 揭阳
攀枝花 敦化 敦煌 文山 文昌 新乐 新乡 新余 新密 新星 新民 新沂 新泰 新郑 无为 无锡 日喀则 日照 旬阳
昆山 昆明 昆玉 昌吉 昌邑 昌都 明光 昭通 晋中 晋城 晋州 晋江 普宁 普洱 景德镇 景洪 曲阜 曲靖 朔州
朝阳 本溪 来宾 杭州 松原 松滋 林州 林芝 枝江 枣庄 枣阳 柳州 栖霞 株洲 根河 格尔木 桂平 桂林 桐乡
桐城 桦甸 梅州 梅河口 梧州 楚雄 榆林 榆树 樟树 横州 武冈 武夷山 武威 武安 武汉 武穴 毕节 水富
永城 永安 永州 永康 永济 汉中 汉川 汕头 汕尾 汝州 江山 江油 江门 江阴 池州 汨罗 汾阳 沁阳 沅江
沈阳 沙河 沙湾 沧州 河池 河津 河源 河间 泉州 泊头 泰兴 泰安 泰州 泸州 泸水 洛阳 津市 洪江 洪湖
洮南 济南 济宁 济源 浏阳 海东 海伦 海口 海城 海宁 海安 海林 海阳 涟源 涿州 淄博 淮北 淮南 淮安
深圳 深州 清远 清镇 温岭 温州 渭南 湖州 湘乡 湘潭 湛江 溧阳 滁州 滕州 满洲里 滦州 滨州 漠河 漯河
漳州 漳平 潍坊 潜山 潜江 潮州 澄江 濮阳 灯塔 灵宝 灵武 烟台 焦作 牙克石 牡丹江 玉林 玉树 玉溪
玉环 玉门 珠海 珲春 琼海 瑞丽 瑞安 瑞昌 瑞金 瓦房店 界首 登封 白城 白山 白杨 白银 百色 益阳 盐城
监利 盖州 盘州 盘锦 眉山 石嘴山 石家庄 石河子 石狮 石首 磐石 祁阳 神木 禄丰 福安 福州 福泉 福清
福鼎 禹城 禹州 秦皇岛 穆棱 简阳 米林 绍兴 绥化 绥芬河 绵竹 绵阳 罗定 老河口 耒阳 聊城 肇东 肇庆
肥城 胡杨河 胶州 腾冲 自贡 舒兰 舞钢 舟山 芒市 芜湖 苏州 英德 茂名 茫崖 荆州 荆门 荔浦 荣成 荥阳
莆田 莱州 莱西 莱阳 菏泽 萍乡 营口 葫芦岛 蒙自 虎林 蚌埠 蛟河 衡水 衡阳 衢州 襄阳 西宁 西安 西昌
讷河 许昌 诸城 诸暨 调兵山 贵港 贵溪 贵阳 贺州 资兴 资阳 赣州 赤壁 赤峰 赤水 辉县 辛集 辽源 辽阳
达州 迁安 运城 连云港 连州 通化 通辽 遂宁 遵义 遵化 邓州 邛崃 邢台 那曲 邯郸 邳州 邵东 邵武 邵阳
邹城 邹平 郑州 郴州 都匀 都江堰 鄂尔多斯 鄂州 酒泉 醴陵 重庆 金华 金昌 钟祥 钦州 铁力 铁岭 铁门关
铜仁 铜川 铜陵 银川 错那 锡林浩特 锦州 镇江 长垣 长春 长沙 长治 长葛 阆中 阜康 阜新 阜阳 防城港
阳春 阳江 阳泉 阿克苏 阿勒泰 阿图什 阿尔山 阿拉尔 阿拉山口 陆丰 陇南 隆昌 随州 雅安 集安 雷州
霍尔果斯 霍州 霍林郭勒 霸州 青岛 青州 青铜峡 靖江 靖西 鞍山 韩城 韶关 韶山 项城 额尔古纳 香格里拉
马尔康 马鞍山 驻马店 高安 高密 高州 高平 高碑店 高邮 鸡西 鹤壁 鹤山 鹤岗 鹰潭 麻城 黄冈 黄山 黄石
黄骅 黑河 黔西 齐齐哈尔 龙井 龙南 龙口 龙岩 龙泉 龙港
香港 澳门 台北 新北 桃园 台中 台南 高雄 基隆 新竹 嘉义
"""
CHINA_CITY_NAMES = frozenset(_CITY_NAMES.split())  # noqa: SIM905

CHINA_CITY_ALIASES = {
    "中国香港": "香港",
    "香港(中国)": "香港",
    "香港（中国）": "香港",
    "香港岛": "香港",
    "新界": "香港",
    "九龙": "香港",
    "hong kong": "香港",
    "中国澳门": "澳门",
    "澳门(中国)": "澳门",
    "澳门（中国）": "澳门",
    "澳门半岛": "澳门",
    "氹仔": "澳门",
    "路环": "澳门",
    "macao": "澳门",
    "macau": "澳门",
}


def standard_china_city_name(name: str) -> str | None:
    """Return the controlled Chinese city name, or ``None`` when out of scope."""

    candidate = CHINA_CITY_ALIASES.get(name, name)
    return candidate if candidate in CHINA_CITY_NAMES else None


def china_city_name_sql(column: str) -> str:
    """Build a SQL expression that applies the controlled aliases."""

    clauses = " ".join(
        f"WHEN {_sql_literal(alias)} THEN {_sql_literal(city)}"
        for alias, city in sorted(CHINA_CITY_ALIASES.items())
    )
    return f"CASE {column} {clauses} ELSE {column} END"


def china_city_values_sql() -> str:
    """Return trusted SQL literals for the controlled city catalog."""

    return ", ".join(_sql_literal(name) for name in sorted(CHINA_CITY_NAMES))


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
