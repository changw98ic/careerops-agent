"""Seed company list for the snowball crawler.

A mix of domestic (Chinese) and international company domains. Each entry is a
bare domain (no scheme/path); the CareerPageFinder infers the career-page URL.

This is the seed set; the snowball crawler discovers more companies from the
pages it crawls (partner links, investor portfolios, industry directories).
Expand by adding more domains — the target is 10,000+.
"""

from __future__ import annotations

# Domestic (Chinese) tech + enterprise companies.
DOMESTIC: tuple[str, ...] = (
    "aliyun.com", "alibaba-inc.com", "tencent.com", "bytedance.com",
    "baidu.com", "meituan.com", "jd.com", "163.com", "mi.com",
    "pinduoduo.com", "didiglobal.com", "kuaishou.com", "bilibili.com",
    "ctrip.com", "dji.com", "sensetime.com", "megvii.com",
    "cambricon.com", "hikvision.com", "nio.com", "lixiang.com",
    "xiaopeng.com", "byd.com", "oppo.com", "vivo.com",
    "lenovo.com", "haier.com", "gree.com", "midea.com",
    "zte.com.cn", "pingan.com", "cmbchina.com", "antgroup.com",
    "ximalaya.com", "keep.com", "smzdm.com", "zhihu.com",
    "weibo.com", "xiaohongshu.com", "douban.com", "qq.com",
    "huawei.com", "honor.cn", "transsion.com", "tcl.com",
    "hisense.com", "changhong.com", "gree.com.cn", "fuyao.com",
    "catl.com", "byd.com", "geely.com", "greatwall.com.cn",
    "saic.com.cn", "baic.com.cn", "changan.com.cn", "dongfeng.com",
    "sf-express.com", "cainiao.com", "yto.net.cn", "sto.cn",
    "ruyi.com", "shopline.com", "shein.com", "temu.com",
    "ai-bot.cn", "cloud.189.cn", "woa.com", "cmcc.com",
    "chinaunicom.com", "chinatelecom.com.cn", "huawei.com",
    "icbc.com.cn", "ccb.com", "boc.cn", "citic.com",
    "cmbchina.com", "spdb.com.cn", "cebbank.com", "cib.com.cn",
    "pingan.com", "huatai.com", "citics.com", "cmschina.com",
    "sinopharm.com", "h3cn.com", "yhdm.com.cn", "kelun.com",
    "hikvision.com", "ehang.com", "ubtech.com", "horizon.cc",
    "4paradigm.com", "istone.com", "datagrand.com", "mininglamp.com",
    "asiainfo.com", "inspur.com", "yonyou.com", "kingdee.com",
    "smartdot.com", "trinasolar.com", "longi.com", "tongwei.com",
    "envision-group.com", "goldenconcord.com", "gcl-power.com",
)

# International tech + enterprise companies.
INTERNATIONAL: tuple[str, ...] = (
    "google.com", "meta.com", "apple.com", "amazon.com",
    "microsoft.com", "netflix.com", "tesla.com", "openai.com",
    "anthropic.com", "stripe.com", "shopify.com", "salesforce.com",
    "adobe.com", "nvidia.com", "intel.com", "amd.com",
    "oracle.com", "sap.com", "siemens.com", "bosch.com",
    "toyota.com", "samsung.com", "sony.com", "tsmc.com",
    "ibm.com", "cisco.com", "vmware.com", "dropbox.com",
    "airbnb.com", "uber.com", "lyft.com", "spotify.com",
    "zoom.us", "atlassian.com", "github.com", "gitlab.com",
    "cloudflare.com", "datadog.com", "hashicorp.com",
    "mongodb.com", "snowflake.com", "databricks.com",
    "palantir.com", "anduril.com", "scale.com",
    "stability.ai", "deepmind.com", "x.ai", "perplexity.ai",
    "mistral.ai", "cohere.com", "huggingface.co",
    "figma.com", "notion.so", "canva.com", "miro.com",
    "asana.com", "monday.com", "clickup.com", "wrike.com",
    "twilio.com", "sendgrid.com", "mailchimp.com",
    "square.com", "paypal.com", "venmo.com", "coinbase.com",
    "binance.com", "kraken.com", "ripple.com", "chain.link",
    "qualcomm.com", "broadcom.com", "texas-instruments.com",
    "netapp.com", "pure-storage.com", "nutanix.com",
    "rackspace.com", "digitalocean.com", "linode.com",
    "heroku.com", "vercel.com", "netlify.com", "render.com",
    "fastly.com", "akamai.com", "edgecast.com",
    "asml.com", "airbus.com", "bmw.com", "mercedes-benz.com",
    "volkswagen.com", "porsche.com", "ferrari.com",
    "shell.com", "bp.com", "total.com", "exxonmobil.com",
    "chevron.com", "pfizer.com", "moderna.com",
    "johnsonandjohnson.com", "novartis.com", "roche.com",
    "siemens-healthineers.com", "ge.com", "3m.com",
    "procter-gamble.com", "unilever.com", "nestle.com",
    "coca-cola.com", "pepsico.com", "nike.com",
    "walmart.com", "costco.com", "target.com",
    "fedex.com", "ups.com", "dhl.com",
    "deloitte.com", "mckinsey.com", "bcg.com",
    "accenture.com", "capgemini.com", "infosys.com",
    "wipro.com", "tata.com", "tcs.com",
    "nintendo.com", "ea.com", "ubisoft.com",
    "valvesoftware.com", "epicgames.com", "riotgames.com",
    "blizzard.com", "mojang.com", "roblox.com",
    "unity.com", "unrealengine.com",
)

ALL_SEEDS: tuple[str, ...] = DOMESTIC + INTERNATIONAL
