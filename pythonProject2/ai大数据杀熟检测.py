# price_audit_vision.py
# 外卖平台图片比价审计引擎 —— 完整整合版
# 覆盖: 美团 / 淘宝闪购 / 京东外卖 / 拼多多 / 抖音外卖
# 模式: 多图同平台对照 + 单图基准库 + 跨平台汇总
# 运行: python price_audit_vision.py
from __future__ import annotations

import hashlib
import json
import os
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Protocol

# ========================= 配置 =========================
MIN_DIFF_RATE = 0.03
CONFIDENCE_THRESHOLD = 0.6
ESTIMATED_TOTAL_TOLERANCE = 1.0
REASONABLE_TAGS = {
    "new_user_promo", "region_cost", "limited_time_promo", "clearance",
}

PLATFORM_ALIASES = {
    "美团": "美团", "美团外卖": "美团",
    "饿了么": "淘宝闪购", "淘宝闪购": "淘宝闪购", "淘宝": "淘宝闪购",
    "京东": "京东外卖", "京东外卖": "京东外卖",
    "拼多多": "拼多多", "多多买菜": "拼多多",
    "抖音": "抖音外卖", "抖音外卖": "抖音外卖",
}

PRODUCT_SUFFIXES = ["套餐", "单人餐", "双人餐", "三人餐", "超值", "特惠"]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def normalize_platform(raw: str) -> str:
    if not raw:
        return "未知"
    raw = raw.strip()
    for k, v in PLATFORM_ALIASES.items():
        if k in raw:
            return v
    return raw

def normalize_product(name: str) -> str:
    if not name:
        return "未知商品"
    n = name.strip()
    for sep in ("（", "(", "【", "["):
        if sep in n:
            n = n.split(sep)[0]
    for suf in PRODUCT_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf):
            n = n[: -len(suf)]
    return n.strip() or name.strip()

# ========================= 数据模型 =========================
@dataclass(frozen=True)
class Persona:
    pid: str
    label: str
    is_member: bool
    order_cnt_90d: int
    device_tier: str = "中端机"
    region: str = "北京"

    @property
    def loyalty_weight(self) -> float:
        if self.order_cnt_90d >= 20: return 1.5
        if self.order_cnt_90d >= 5:  return 1.2
        return 1.0

@dataclass
class Quote:
    sku_id: str
    persona_id: str
    list_price: float
    coupon_discount: float = 0.0
    member_discount: float = 0.0
    delivery_fee: float = 0.0
    packaging_fee: float = 0.0
    estimated_total: Optional[float] = None
    promo_tag: Optional[str] = None
    platform: str = ""
    product_name: str = ""
    collected_at: str = field(default_factory=now_iso)
    source_image: str = ""

    @property
    def final_price(self) -> float:
        return round(self.list_price + self.delivery_fee + self.packaging_fee
                     - self.coupon_discount - self.member_discount, 2)

    @property
    def total_mismatch(self) -> Optional[float]:
        if self.estimated_total is None:
            return None
        return round(self.estimated_total - self.final_price, 2)

@dataclass
class Finding:
    sku_id: str
    persona_id: str
    persona_label: str
    final_price: float
    base_price: float
    diff_rate: float
    risk_score: float
    reason: str
    source_image: str = ""
    total_mismatch: Optional[float] = None

@dataclass
class ExtractedPrice:
    platform: str
    product_name: str
    list_price: float
    coupon_discount: float = 0.0
    member_discount: float = 0.0
    delivery_fee: float = 0.0
    packaging_fee: float = 0.0
    estimated_total: Optional[float] = None
    user_label: str = "未知"
    promo_tag: Optional[str] = None
    captured_at: Optional[str] = None
    raw_text: str = ""
    confidence: float = 0.0
    image_path: str = ""

    def to_persona(self, pid: str) -> Persona:
        label = self.user_label
        is_member = "会员" in label
        if "老" in label:
            cnt = 30
        elif is_member:
            cnt = 45
        elif "新" in label:
            cnt = 0
        else:
            cnt = 2
        return Persona(pid=pid, label=label, is_member=is_member, order_cnt_90d=cnt)

    def to_quote(self, persona_id: str, sku_id: str) -> Quote:
        return Quote(
            sku_id=sku_id, persona_id=persona_id,
            list_price=self.list_price,
            coupon_discount=self.coupon_discount,
            member_discount=self.member_discount,
            delivery_fee=self.delivery_fee,
            packaging_fee=self.packaging_fee,
            estimated_total=self.estimated_total,
            promo_tag=self.promo_tag,
            platform=normalize_platform(self.platform),
            product_name=normalize_product(self.product_name),
            collected_at=self.captured_at or now_iso(),
            source_image=self.image_path,
        )

# ========================= 提取器 =========================
class ImageExtractor(Protocol):
    def extract(self, image_path: str) -> ExtractedPrice: ...

class MockImageExtractor:
    _DATA = {
        # 美团组：3 个画像，构成有效对照组
        "meituan_old.jpg": ExtractedPrice(
            platform="美团", product_name="麻辣香锅套餐",
            list_price=34.0, delivery_fee=3.0, packaging_fee=2.0,
            coupon_discount=0.0, member_discount=0.0, estimated_total=39.0,
            user_label="老用户", promo_tag=None,
            captured_at="2026-10-28 12:00:00",
            raw_text="麻辣香锅 标价¥34 配送¥3 打包¥2 无可用券 到手¥39",
            confidence=0.95, image_path="meituan_old.jpg",
        ),
        "meituan_new.jpg": ExtractedPrice(
            platform="美团", product_name="麻辣香锅套餐",
            list_price=34.0, delivery_fee=3.0, packaging_fee=2.0,
            coupon_discount=10.0, member_discount=0.0, estimated_total=29.0,
            user_label="新用户", promo_tag="new_user_promo",
            captured_at="2026-10-28 12:00:05",
            raw_text="新客立减¥10 到手¥29",
            confidence=0.93, image_path="meituan_new.jpg",
        ),
        "meituan_member.jpg": ExtractedPrice(
            platform="美团", product_name="麻辣香锅套餐",
            list_price=35.0, delivery_fee=0.0, packaging_fee=2.0,
            coupon_discount=0.0, member_discount=0.0, estimated_total=37.0,
            user_label="付费会员", promo_tag=None,
            captured_at="2026-10-28 12:00:10",
            raw_text="会员免配送费 到手¥37",
            confidence=0.94, image_path="meituan_member.jpg",
        ),
        # 跨平台样本：各 1 张，只进入跨平台汇总
        "taobao_new.jpg": ExtractedPrice(
            platform="饿了么", product_name="麻辣香锅",
            list_price=30.0, delivery_fee=2.0, packaging_fee=1.0,
            coupon_discount=10.0, member_discount=0.0, estimated_total=23.0,
            user_label="新用户", promo_tag="new_user_promo",
            captured_at="2026-10-28 12:05:00",
            raw_text="闪购新客减¥10 到手¥23",
            confidence=0.92, image_path="taobao_new.jpg",
        ),
        "jd_member.jpg": ExtractedPrice(
            platform="京东", product_name="麻辣香锅",
            list_price=35.0, delivery_fee=0.0, packaging_fee=2.0,
            coupon_discount=0.0, member_discount=2.0, estimated_total=35.0,
            user_label="付费会员", promo_tag=None,
            captured_at="2026-10-28 12:10:00",
            raw_text="PLUS会员立减¥2 到手¥35",
            confidence=0.91, image_path="jd_member.jpg",
        ),
    }

    def extract(self, image_path: str) -> ExtractedPrice:
        if image_path not in self._DATA:
            raise FileNotFoundError(
                f"Mock 中无此图片: {image_path}\n可用: {list(self._DATA)}"
            )
        return self._DATA[image_path]

class VisionAPIExtractor:
    """真实接入：OpenAI 兼容接口，支持 GPT-4o / Qwen-VL / GLM-4V / 豆包视觉
    安装: pip install openai
    环境变量: VISION_API_KEY  必填
              VISION_BASE_URL 选填（默认 OpenAI）
              VISION_MODEL    选填（默认 gpt-4o-mini）
    """

    PROMPT = """你是外卖平台截图价格提取专家。请识别截图来源并提取以下严格JSON：

{
  "platform": "美团/淘宝闪购/京东外卖/拼多多/抖音外卖 之一，或识别到的原始名称",
  "product_name": "主商品名称，如「麻辣香锅套餐」",
  "list_price": 数字,          // 商品标价或划线价
  "coupon_discount": 数字,     // 优惠券/满减/红包/立减 合计
  "member_discount": 数字,     // 会员专享折扣/会员红包
  "delivery_fee": 数字,        // 配送费，免配送填0
  "packaging_fee": 数字,       // 打包费
  "estimated_total": 数字或null, // 页面显示的「到手价/预估到手」
  "user_label": "新用户/老用户/付费会员/未知",
  "promo_tag": "new_user_promo/region_cost/limited_time_promo/clearance/null",
  "captured_at": "截图时间字符串或null",
  "confidence": 0到1
}

识别线索：
- 美团：黄色袋鼠图标、"堂食店"标签
- 淘宝闪购：橙色"闪购"标识（原饿了么）
- 京东外卖：红色"品质外卖"、"PLUS会员"
- 拼多多：红色"多多买菜/拼好饭"
- 抖音外卖：黑色音符图标

价格注意：标价与到手价可能不同，务必分别提取；满减、店铺券、平台券都计入 coupon_discount；会员红包、PLUS立减计入 member_discount。看不清的字段给0或null，不要猜测。"""

    def __init__(self):
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("请先安装: pip install openai")
        key = os.environ.get("VISION_API_KEY")
        if not key:
            raise RuntimeError("请设置环境变量 VISION_API_KEY")
        self.client = OpenAI(
            api_key=key,
            base_url=os.environ.get("VISION_BASE_URL", "https://api.openai.com/v1"),
        )
        self.model = os.environ.get("VISION_MODEL", "gpt-4o-mini")

    def extract(self, image_path: str) -> ExtractedPrice:
        import base64
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ext = image_path.rsplit(".", 1)[-1].lower()
        mime = "image/png" if ext == "png" else "image/jpeg"

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            temperature=0,
        )
        content = resp.choices[0].message.content.strip()
        for p in ("```json", "```"):
            content = content.removeprefix(p)
        content = content.removesuffix("```").strip()

        try:
            d = json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(f"视觉模型返回非JSON: {content[:200]}")

        def fnum(v):
            try:
                return float(v) if v not in (None, "", "null") else 0.0
            except (TypeError, ValueError):
                return 0.0

        et = d.get("estimated_total")
        return ExtractedPrice(
            platform=d.get("platform", "未知"),
            product_name=d.get("product_name", "未知商品"),
            list_price=fnum(d.get("list_price")),
            coupon_discount=fnum(d.get("coupon_discount")),
            member_discount=fnum(d.get("member_discount")),
            delivery_fee=fnum(d.get("delivery_fee")),
            packaging_fee=fnum(d.get("packaging_fee")),
            estimated_total=fnum(et) if et not in (None, "", "null") else None,
            user_label=d.get("user_label", "未知"),
            promo_tag=d.get("promo_tag"),
            captured_at=d.get("captured_at"),
            raw_text=content,
            confidence=fnum(d.get("confidence")),
            image_path=image_path,
        )

# ========================= 检测逻辑 =========================
class FairnessDetector:
    def detect(self, sku_id: str, quotes: list[Quote],
               personas: dict[str, Persona]) -> list[Finding]:
        if len(quotes) < 2:
            return []
        prices = [q.final_price for q in quotes]
        base = statistics.median(prices)
        if base <= 0:
            return []

        out: list[Finding] = []
        for q in quotes:
            p = personas[q.persona_id]
            diff = (q.final_price - base) / base
            if diff < MIN_DIFF_RATE or q.promo_tag in REASONABLE_TAGS:
                continue
            score = min(round(diff * 100 * p.loyalty_weight
                              * (1.3 if p.is_member else 1.0), 1), 100.0)
            parts = [f"实付价比同平台同商品中位价高 {diff:.1%}"]
            if p.is_member:
                parts.append("付费会员未享受价格优势")
            if p.order_cnt_90d >= 20:
                parts.append(f"高频忠诚用户（90天{p.order_cnt_90d}单）")
            if q.coupon_discount == 0:
                parts.append("未获得任何优惠券")
            if not q.promo_tag:
                parts.append("无合理促销因素说明")
            out.append(Finding(
                sku_id=sku_id, persona_id=p.pid, persona_label=p.label,
                final_price=q.final_price, base_price=round(base, 2),
                diff_rate=round(diff, 4), risk_score=score,
                reason="；".join(parts), source_image=q.source_image,
                total_mismatch=q.total_mismatch,
            ))
        return out

class CouponFairnessChecker:
    def check(self, quotes: list[Quote],
              personas: dict[str, Persona]) -> Optional[dict]:
        if len(quotes) < 2:
            return None
        new_u = [q for q in quotes if personas[q.persona_id].order_cnt_90d <= 1]
        old_u = [q for q in quotes if personas[q.persona_id].order_cnt_90d >= 10]
        if not new_u or not old_u:
            return None
        a_new = statistics.mean(q.coupon_discount for q in new_u)
        a_old = statistics.mean(q.coupon_discount for q in old_u)
        if a_new - a_old <= 0:
            return None
        return {
            "type": "差异化发券",
            "new_user_avg_coupon": round(a_new, 2),
            "old_user_avg_coupon": round(a_old, 2),
            "gap": round(a_new - a_old, 2),
            "note": "老用户平均券额显著低于新用户",
        }

# ========================= 存证 =========================
def pack_evidence(payload: dict) -> dict:
    canonical = json.dumps(payload, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return {
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "timestamp": now_iso(),
        "algorithm": "SHA-256",
    }

# ========================= 多图比价 =========================
def audit_multi_images(paths: list[str], extractor: ImageExtractor) -> dict:
    extracted: list[ExtractedPrice] = []
    skipped: list[dict] = []

    for p in paths:
        try:
            e = extractor.extract(p)
        except Exception as ex:
            skipped.append({"image": p, "reason": str(ex)})
            continue
        if e.confidence < CONFIDENCE_THRESHOLD:
            skipped.append({
                "image": p,
                "reason": f"识别置信度 {e.confidence:.2f} 低于阈值 {CONFIDENCE_THRESHOLD}",
            })
            continue
        extracted.append(e)

    if not extracted:
        return {"error": "没有可用的识别结果", "skipped": skipped}

    # 按 (平台, 商品) 分组：只有同平台同商品才构成有效杀熟对照组
    groups: dict[tuple[str, str], list[ExtractedPrice]] = {}
    for e in extracted:
        key = (normalize_platform(e.platform), normalize_product(e.product_name))
        groups.setdefault(key, []).append(e)

    detector = FairnessDetector()
    coupon_chk = CouponFairnessChecker()
    group_results: list[dict] = []
    cross_platform: dict[str, dict[str, dict]] = {}

    for (platform, product), items in groups.items():
        personas, quotes = {}, []
        for i, e in enumerate(items):
            pid = f"P{i+1}"
            personas[pid] = e.to_persona(pid)
            quotes.append(e.to_quote(pid, product))

        findings = detector.detect(product, quotes, personas)
        coupon = coupon_chk.check(quotes, personas)
        group_results.append({
            "platform": platform, "product": product,
            "quotes": quotes, "personas": personas,
            "findings": findings, "coupon_issue": coupon,
        })

        cp = cross_platform.setdefault(product, {})
        cp.setdefault(platform, {"min_price": None, "count": 0, "images": []})
        for q in quotes:
            entry = cp[platform]
            entry["min_price"] = q.final_price if entry["min_price"] is None \
                else min(entry["min_price"], q.final_price)
            entry["count"] += 1
            entry["images"].append(q.source_image)

    return {
        "mode": "multi",
        "groups": group_results,
        "cross_platform": cross_platform,
        "skipped": skipped,
    }

# ========================= 单图 + 基准库 =========================
BASELINE: dict[str, dict] = {
    "麻辣香锅": {
        "美团":     {"median_price": 35.0, "sample_size": 128},
        "淘宝闪购": {"median_price": 28.0, "sample_size": 96},
        "京东外卖": {"median_price": 34.0, "sample_size": 42},
        "_default": {"median_price": 33.0, "sample_size": 266},
    },
}

def audit_single_image(path: str, extractor: ImageExtractor) -> dict:
    try:
        e = extractor.extract(path)
    except Exception as ex:
        return {"error": str(ex)}

    if e.confidence < CONFIDENCE_THRESHOLD:
        return {"error": f"识别置信度 {e.confidence:.2f} 低于阈值，需人工核对"}

    platform = normalize_platform(e.platform)
    product = normalize_product(e.product_name)

    product_baseline = BASELINE.get(product)
    if not product_baseline:
        return {"error": f"基准库中没有「{product}」，请改传多张同平台对照图"}

    b = product_baseline.get(platform) or product_baseline.get("_default")
    if not b:
        return {"error": f"「{product}」在 {platform} 无基准数据"}

    final = round(e.list_price + e.delivery_fee + e.packaging_fee
                  - e.coupon_discount - e.member_discount, 2)
    diff = (final - b["median_price"]) / b["median_price"]
    reasonable = e.promo_tag in REASONABLE_TAGS
    suspected = diff >= MIN_DIFF_RATE and not reasonable
    score = min(round(max(diff, 0) * 100 * 1.2, 1), 100.0) if suspected else 0.0

    mismatch = None
    if e.estimated_total is not None:
        mismatch = round(e.estimated_total - final, 2)

    return {
        "mode": "single",
        "image": path,
        "platform": platform,
        "product": product,
        "user_label": e.user_label,
        "final_price": final,
        "breakdown": {
            "list_price": e.list_price,
            "delivery_fee": e.delivery_fee,
            "packaging_fee": e.packaging_fee,
            "coupon_discount": e.coupon_discount,
            "member_discount": e.member_discount,
        },
        "baseline_price": b["median_price"],
        "baseline_sample_size": b["sample_size"],
        "diff_rate": round(diff, 4),
        "promo_tag": e.promo_tag,
        "reasonable": reasonable,
        "suspected": suspected,
        "risk_score": score,
        "total_mismatch": mismatch,
        "evidence": pack_evidence({
            "image": path, "extracted": asdict(e),
            "baseline": b, "final": final,
        }),
    }

# ========================= 报告输出 =========================
def print_multi(result: dict) -> None:
    if "error" in result:
        print(f"[错误] {result['error']}")
        for s in result.get("skipped", []):
            print(f"  - 跳过 {s['image']}: {s['reason']}")
        return

    print("【同平台对照组检测】")
    any_finding = False
    for g in result["groups"]:
        if len(g["quotes"]) < 2:
            continue
        print("=" * 78)
        print(f"平台: {g['platform']}   商品: {g['product']}")
        print("-" * 78)
        print(f"{'画像':<10}{'标价':>8}{'配送':>7}{'打包':>7}"
              f"{'券':>7}{'会员减':>8}{'实付':>8}   来源")
        for q in sorted(g["quotes"], key=lambda x: x.final_price):
            label = g["personas"][q.persona_id].label
            print(f"{label:<10}{q.list_price:>8.2f}{q.delivery_fee:>7.2f}"
                  f"{q.packaging_fee:>7.2f}{q.coupon_discount:>7.2f}"
                  f"{q.member_discount:>8.2f}{q.final_price:>8.2f}"
                  f"   {q.source_image}")
        for f in g["findings"]:
            any_finding = True
            print(f"[!] {f.persona_label} 高出中位价 {f.diff_rate:.1%} "
                  f"| 风险分 {f.risk_score}")
            print(f"    理由: {f.reason}")
            if f.total_mismatch is not None and abs(f.total_mismatch) > ESTIMATED_TOTAL_TOLERANCE:
                print(f"    注意: 页面到手价与费用项加总差异 {f.total_mismatch:+.2f} 元，需人工核查")
        if g["coupon_issue"]:
            any_finding = True
            c = g["coupon_issue"]
            print(f"[!] {c['type']}: 新客均券 {c['new_user_avg_coupon']} 元 "
                  f"vs 老客 {c['old_user_avg_coupon']} 元（差 {c['gap']} 元）")
        if not g["findings"] and not g["coupon_issue"]:
            print("[✓] 未发现疑似价格歧视")
        ev = pack_evidence({
            "platform": g["platform"], "product": g["product"],
            "prices": [q.final_price for q in g["quotes"]],
        })
        print(f"\n证据哈希: {ev['sha256'][:32]}...  时间: {ev['timestamp']}")
        print("=" * 78)

    if not any_finding:
        print("\n[✓] 所有同平台对照组均未发现疑似价格歧视")

    print("\n【跨平台价格对比】（仅供参考，不判定杀熟）")
    for product, platforms in result["cross_platform"].items():
        print("-" * 78)
        print(f"商品: {product}")
        print(f"{'平台':<12}{'最低实付价':>12}{'样本数':>8}   来源")
        for plat, info in sorted(platforms.items(),
                                 key=lambda kv: kv[1]["min_price"] or 0):
            print(f"{plat:<12}{info['min_price']:>12.2f}{info['count']:>8}"
                  f"   {', '.join(info['images'])}")

    if result["skipped"]:
        print("\n【跳过项】")
        for s in result["skipped"]:
            print(f"  - {s['image']}: {s['reason']}")

def print_single(r: dict) -> None:
    if "error" in r:
        print(f"[错误] {r['error']}")
        return
    print("=" * 78)
    print(f"图片: {r['image']}")
    print(f"平台: {r['platform']}   商品: {r['product']}   画像: {r['user_label']}")
    print("-" * 78)
    b = r["breakdown"]
    print(f"标价 {b['list_price']:.2f} + 配送 {b['delivery_fee']:.2f} "
          f"+ 打包 {b['packaging_fee']:.2f} - 券 {b['coupon_discount']:.2f} "
          f"- 会员 {b['member_discount']:.2f} = 实付 {r['final_price']:.2f}")
    print(f"基准中位价: {r['baseline_price']:.2f}（样本 {r['baseline_sample_size']}）"
          f"   差异: {r['diff_rate']:+.1%}")
    print(f"促销标签: {r['promo_tag'] or '-'}   合理因素: {'是' if r['reasonable'] else '否'}")
    if r["suspected"]:
        print(f"结论: 疑似价格歧视，风险分 {r['risk_score']}")
    else:
        print("结论: 未发现异常")
    if r["total_mismatch"] is not None and abs(r["total_mismatch"]) > ESTIMATED_TOTAL_TOLERANCE:
        print(f"注意: 页面到手价与费用项加总差异 {r['total_mismatch']:+.2f} 元，需人工核查")
    print(f"证据哈希: {r['evidence']['sha256'][:32]}...")
    print("=" * 78)

# ========================= 主流程 =========================
def main() -> None:
    # 默认 Mock。接真实视觉模型：
    #   extractor = VisionAPIExtractor()
    extractor: ImageExtractor = MockImageExtractor()

    print("\n########## 模式一：多图对照比价 ##########\n")
    multi = audit_multi_images(
        [
            "meituan_old.jpg", "meituan_new.jpg", "meituan_member.jpg",
            "taobao_new.jpg", "jd_member.jpg",
        ],
        extractor,
    )
    print_multi(multi)

    print("\n########## 模式二：单图 + 基准库 ##########\n")
    single = audit_single_image("meituan_old.jpg", extractor)
    print_single(single)

    # 导出 JSON 报告
    out = {
        "generated_at": now_iso(),
        "multi": multi,
        "single": single,
    }
    with open("audit_vision_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n[导出] 审计报告已写入 audit_vision_report.json")

    print("\n【合规提示】")
    print("1. 图片识别结果须人工核对，低置信度样本不得直接作为举报依据。")
    print("2. 杀熟判定必须基于「同平台、同商品、不同画像」的对照；跨平台价差只做参考。")
    print("3. 截图须为用户本人真实账号、合法可见页面，不得伪造或盗用他人账号。")
    print("4. 本工具只输出客观数据与技术分析，不代替执法机关认定违法。")

if __name__ == "__main__":
    main()
