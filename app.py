import { useState, useEffect, useCallback, useRef } from "react";

const CAP_TIERS  = { Nano:0, Micro:1, Small:2, Mid:3, Large:4, ETF:2 };
const CAP_COLORS = { Nano:"#ea80fc", Micro:"#ff6d00", Small:"#ffd600", Mid:"#00e676", Large:"#00b0ff", ETF:"#90caf9" };
const RISK_CFG   = {
  CRITICAL:{ label:"CRITICAL",    color:"#ff1744", bg:"#160004" },
  HIGH:    { label:"HIGH RISK",   color:"#ff6d00", bg:"#160900" },
  MODERATE:{ label:"MODERATE",    color:"#ffd600", bg:"#141000" },
  POSITIVE:{ label:"OPPORTUNITY", color:"#00e676", bg:"#00160a" },
  STRONG:  { label:"STRONG",      color:"#00b0ff", bg:"#00091a" },
};
const TF_PROFILES = {
  "⚡ Intraday":    { short:"0–24h",   color:"#ff6d00", icon:"⚡", traderType:"Day trader",       timeCommit:"Active monitoring",  simDays:0.5  },
  "📈 Short Swing": { short:"2–5d",    color:"#ffd600", icon:"📈", traderType:"Part-time swing",  timeCommit:"Check 1–2× daily",   simDays:3.5  },
  "🌊 Medium Swing":{ short:"1–4wk",   color:"#00d4ff", icon:"🌊", traderType:"Hobbyist ★",       timeCommit:"2–3× per week",      simDays:17.5 },
  "🏔️ Position":    { short:"1–6mo",   color:"#00e676", icon:"🏔️", traderType:"Growth investor",  timeCommit:"Monthly review",     simDays:105  },
  "🌳 Long Term":   { short:"6mo–3yr", color:"#aed581", icon:"🌳", traderType:"Wealth compounder", timeCommit:"Quarterly review",   simDays:548  },
};
const WATCH_DURATIONS = [
  { label:"0–24 hrs", group:"Intraday", realMs:24*3600000,   simDays:0.5,  tf:"⚡ Intraday"    },
  { label:"2 days",   group:"Short",   realMs:2*86400000,   simDays:2,    tf:"📈 Short Swing"  },
  { label:"3 days",   group:"Short",   realMs:3*86400000,   simDays:3,    tf:"📈 Short Swing"  },
  { label:"4 days",   group:"Short",   realMs:4*86400000,   simDays:4,    tf:"📈 Short Swing"  },
  { label:"5 days",   group:"Short",   realMs:5*86400000,   simDays:5,    tf:"📈 Short Swing"  },
  { label:"1 week",   group:"Swing",   realMs:7*86400000,   simDays:7,    tf:"🌊 Medium Swing"  },
  { label:"2 weeks",  group:"Swing",   realMs:14*86400000,  simDays:14,   tf:"🌊 Medium Swing"  },
  { label:"3 weeks",  group:"Swing",   realMs:21*86400000,  simDays:21,   tf:"🌊 Medium Swing"  },
  { label:"4 weeks",  group:"Swing",   realMs:28*86400000,  simDays:28,   tf:"🌊 Medium Swing"  },
  { label:"1 month",  group:"Position",realMs:30*86400000,  simDays:30,   tf:"🏔️ Position"      },
  { label:"2 months", group:"Position",realMs:60*86400000,  simDays:60,   tf:"🏔️ Position"      },
  { label:"3 months", group:"Position",realMs:90*86400000,  simDays:90,   tf:"🏔️ Position"      },
  { label:"4 months", group:"Position",realMs:120*86400000, simDays:120,  tf:"🏔️ Position"      },
  { label:"5 months", group:"Position",realMs:150*86400000, simDays:150,  tf:"🏔️ Position"      },
  { label:"6 months", group:"Position",realMs:180*86400000, simDays:180,  tf:"🏔️ Position"      },
];
const WATCH_GROUPS = ["Intraday","Short","Swing","Position"];
const GROUP_COLORS = { Intraday:"#ff6d00", Short:"#ffd600", Swing:"#00d4ff", Position:"#00e676" };

function fmtDuration(ms) {
  if(ms<0) return "expired";
  const h=Math.floor(ms/3600000),d=Math.floor(ms/86400000),w=Math.floor(d/7),mo=Math.floor(d/30);
  if(mo>=1) return `${mo}mo`; if(w>=1) return `${w}wk`; if(d>=1) return `${d}d`; return `${h}h`;
}
const TF_KEYS = Object.keys(TF_PROFILES);

const TRADE_ADVICE = {
  timeframe:[
    {icon:"⏱️",tag:"TIMEFRAME SELECTION",title:"Match the window to your life",body:"The most successful hobbyist traders pick timeframes that fit their schedule — not the market's pace. A medium swing (1–4 weeks) requires just 2–3 checks per week. Intraday trading requires active monitoring you may not have.",color:"#00d4ff"},
    {icon:"🎯",tag:"BEST FIT SIGNAL",title:"Higher alignment = better probability conditions",body:"The signal alignment % tells you how well current market conditions match historical patterns for that timeframe. It is NOT a guarantee — markets are not deterministic. 72%+ means strong conditions. Below 35% means the window may not be ideal right now.",color:"#00e676"},
    {icon:"📅",tag:"COMMITMENT WARNING",title:"Only trade what you can monitor",body:"Placing an intraday trade then walking away is one of the most common beginner mistakes. If you can't watch it, don't trade that timeframe. The medium swing or position window suits most people with jobs and limited trading time.",color:"#ffd600"},
  ],
  sizing:[
    {icon:"📐",tag:"2% RULE",title:"The most important rule in trading",body:"Risk no more than 2% of your account on any single trade. On a £1,000 account that's £20. This means you can lose 10 trades in a row and still have 82% of your account intact — giving you time to learn and improve.",color:"#00e676"},
    {icon:"🧮",tag:"POSITION SIZE FORMULA",title:"Shares = Risk ÷ (Entry − Stop)",body:"Position size = (Account × 2%) ÷ (Entry price × Stop%). The tool calculates this for you — trust the formula, not your gut.",color:"#00b0ff"},
    {icon:"⚠️",tag:"CONCENTRATION RISK",title:"Never put more than 30% in one trade",body:"Even if signals look perfect, limiting single positions to 30% of your account prevents one bad trade from doing lasting damage.",color:"#ff9800"},
  ],
  rr:[
    {icon:"⚖️",tag:"REWARD:RISK RATIO",title:"2:1 is the minimum — always",body:"If you risk £20, your potential gain must be at least £40 before the trade makes mathematical sense. Over 50 trades with a 50% win rate and 2:1 R/R, you're profitable.",color:"#00e676"},
    {icon:"✂️",tag:"CUT LOSSES FAST",title:"Your stop loss is not optional",body:"The stop loss is set before the trade, followed without negotiation. Moving a stop loss lower to avoid taking a loss is one of the most destructive habits in trading.",color:"#ff1744"},
    {icon:"🚀",tag:"LET WINNERS RUN",title:"Most profits come from a few big winners",body:"Inexperienced traders take profits too early and hold losses too long. Use a trailing stop for winners, and exit at your predefined stop for losers.",color:"#ea80fc"},
  ],
  entry:[
    {icon:"🔍",tag:"ENTRY QUALITY",title:"IDEAL and GOOD entries matter most",body:"IDEAL entry: price near support, RSI oversold (<35), volume starting to expand. GOOD: price below 50-day MA, volume above average. POOR: price extended above recent highs, RSI above 70.",color:"#00e676"},
    {icon:"📊",tag:"VOLUME CONFIRMATION",title:"Price moves without volume are suspect",body:"Any breakout or bounce must be accompanied by above-average volume to be reliable. A price spike on low volume often reverses quickly.",color:"#00d4ff"},
    {icon:"🏦",tag:"INSIDER SIGNAL",title:"Watch for Form 4 insider buying",body:"When company insiders buy shares in the open market, they're putting their own money at risk — a powerful signal.",color:"#ffd600"},
  ],
  checklist:[
    {icon:"📝",tag:"PRE-TRADE PLAN",title:"Write it down before you click confirm",body:"Profitable traders write: (1) Why am I entering? (2) Where's my stop? (3) What's my target? (4) How many shares? If you can't answer all four clearly, don't take the trade.",color:"#00b0ff"},
    {icon:"🧠",tag:"EMOTIONAL DISCIPLINE",title:"Is this a setup or FOMO?",body:"Before confirming, ask: am I entering because the setup is valid, or because I'm afraid of missing a move? FOMO trades — entered because a stock has already run — have inverted risk/reward.",color:"#ffd600"},
    {icon:"🌱",tag:"SEED CAPITAL MINDSET",title:"Your first £1k is a learning fund",body:"The goal of a small account is NOT to get rich quickly. It's to build discipline, learn signal reading, and grow toward £10k through a combination of good returns AND regular contributions.",color:"#00e676"},
  ],
};

// ── CURRENCIES ──────────────────────────────────────────────
const CURRENCIES = [
  { code:"GBP", symbol:"£",   flag:"🇬🇧", name:"British Pound",      rate:1.00   },
  { code:"USD", symbol:"$",   flag:"🇺🇸", name:"US Dollar",          rate:1.27   },
  { code:"EUR", symbol:"€",   flag:"🇪🇺", name:"Euro",               rate:1.17   },
  { code:"JPY", symbol:"¥",   flag:"🇯🇵", name:"Japanese Yen",       rate:192.5  },
  { code:"CAD", symbol:"C$",  flag:"🇨🇦", name:"Canadian Dollar",    rate:1.72   },
  { code:"AUD", symbol:"A$",  flag:"🇦🇺", name:"Australian Dollar",  rate:1.95   },
  { code:"CHF", symbol:"₣",   flag:"🇨🇭", name:"Swiss Franc",        rate:1.13   },
  { code:"INR", symbol:"₹",   flag:"🇮🇳", name:"Indian Rupee",       rate:105.8  },
  { code:"SGD", symbol:"S$",  flag:"🇸🇬", name:"Singapore Dollar",   rate:1.71   },
  { code:"HKD", symbol:"HK$", flag:"🇭🇰", name:"Hong Kong Dollar",   rate:9.93   },
  { code:"NOK", symbol:"kr",  flag:"🇳🇴", name:"Norwegian Krone",    rate:13.4   },
  { code:"SEK", symbol:"kr",  flag:"🇸🇪", name:"Swedish Krona",      rate:13.1   },
  { code:"NZD", symbol:"NZ$", flag:"🇳🇿", name:"New Zealand Dollar", rate:2.12   },
  { code:"ZAR", symbol:"R",   flag:"🇿🇦", name:"South African Rand", rate:23.5   },
  { code:"AED", symbol:"د.إ",flag:"🇦🇪", name:"UAE Dirham",         rate:4.66   },
  { code:"BRL", symbol:"R$",  flag:"🇧🇷", name:"Brazilian Real",     rate:6.37   },
  { code:"MXN", symbol:"$",   flag:"🇲🇽", name:"Mexican Peso",       rate:21.9   },
  { code:"KRW", symbol:"₩",   flag:"🇰🇷", name:"South Korean Won",   rate:1680   },
  { code:"CNY", symbol:"¥",   flag:"🇨🇳", name:"Chinese Yuan",       rate:9.22   },
  { code:"SEK", symbol:"kr",  flag:"🇸🇪", name:"Swedish Krona",      rate:13.1   },
];

// ── ASSETS ───────────────────────────────────────────────────
const ASSETS = [
  { ticker:"NVDA", name:"NVIDIA",              sector:"Technology", sub:"Semiconductors",  cap:"Large",  priceRange:[800,950],  volatility:"Low"    },
  { ticker:"SLAB", name:"Silicon Labs",        sector:"Technology", sub:"Semiconductors",  cap:"Mid",    priceRange:[80,140],   volatility:"Med"    },
  { ticker:"WOLF", name:"Wolfspeed",           sector:"Technology", sub:"Semiconductors",  cap:"Small",  priceRange:[3,18],     volatility:"High"   },
  { ticker:"AEHR", name:"Aehr Test Systems",   sector:"Technology", sub:"Semiconductors",  cap:"Micro",  priceRange:[6,22],     volatility:"High"   },
  { ticker:"SOUN", name:"SoundHound AI",       sector:"Technology", sub:"AI / ML",         cap:"Micro",  priceRange:[3,18],     volatility:"VHigh"  },
  { ticker:"BBAI", name:"BigBear.ai",          sector:"Technology", sub:"AI / ML",         cap:"Micro",  priceRange:[1,8],      volatility:"VHigh"  },
  { ticker:"AITX", name:"AI Technology Inc",   sector:"Technology", sub:"AI / ML",         cap:"Nano",   priceRange:[0.05,0.5], volatility:"Extreme"},
  { ticker:"RCAT", name:"Red Cat Holdings",    sector:"Technology", sub:"Robotics",        cap:"Micro",  priceRange:[3,20],     volatility:"VHigh"  },
  { ticker:"CRWD", name:"CrowdStrike",         sector:"Technology", sub:"Cybersecurity",   cap:"Large",  priceRange:[200,380],  volatility:"Med"    },
  { ticker:"IONQ", name:"IonQ",               sector:"Technology", sub:"Quantum",         cap:"Small",  priceRange:[6,40],     volatility:"High"   },
  { ticker:"QUBT", name:"Quantum Computing",   sector:"Technology", sub:"Quantum",         cap:"Nano",   priceRange:[0.5,8],    volatility:"Extreme"},
  { ticker:"MRNA", name:"Moderna",             sector:"Healthcare", sub:"Pharma",          cap:"Mid",    priceRange:[35,130],   volatility:"High"   },
  { ticker:"NVAX", name:"Novavax",             sector:"Healthcare", sub:"Pharma",          cap:"Small",  priceRange:[4,30],     volatility:"VHigh"  },
  { ticker:"AGEN", name:"Agenus Inc",          sector:"Healthcare", sub:"Biotech",         cap:"Micro",  priceRange:[0.5,5],    volatility:"Extreme"},
  { ticker:"IMVT", name:"Immunovant",          sector:"Healthcare", sub:"Rare Disease",    cap:"Small",  priceRange:[20,60],    volatility:"High"   },
  { ticker:"RXRX", name:"Recursion Pharma",    sector:"Healthcare", sub:"Biotech",         cap:"Small",  priceRange:[3,20],     volatility:"High"   },
  { ticker:"NTLA", name:"Intellia",            sector:"Healthcare", sub:"CRISPR",          cap:"Small",  priceRange:[10,55],    volatility:"High"   },
  { ticker:"EDIT", name:"Editas Medicine",     sector:"Healthcare", sub:"CRISPR",          cap:"Micro",  priceRange:[2,18],     volatility:"VHigh"  },
  { ticker:"TDOC", name:"Teladoc",             sector:"Healthcare", sub:"Telehealth",      cap:"Mid",    priceRange:[8,40],     volatility:"Med"    },
  { ticker:"XOM",  name:"ExxonMobil",          sector:"Energy",     sub:"Oil & Gas",       cap:"Large",  priceRange:[95,130],   volatility:"Low"    },
  { ticker:"REI",  name:"Ring Energy",         sector:"Energy",     sub:"Exploration",     cap:"Small",  priceRange:[3,12],     volatility:"High"   },
  { ticker:"PLUG", name:"Plug Power",          sector:"Energy",     sub:"Hydrogen",        cap:"Small",  priceRange:[1,15],     volatility:"VHigh"  },
  { ticker:"NNE",  name:"Nano Nuclear Energy", sector:"Energy",     sub:"Nuclear",         cap:"Micro",  priceRange:[5,45],     volatility:"Extreme"},
  { ticker:"BLNK", name:"Blink Charging",      sector:"Energy",     sub:"EV Charging",     cap:"Micro",  priceRange:[1,18],     volatility:"VHigh"  },
  { ticker:"LAC",  name:"Lithium Americas",    sector:"Materials",  sub:"Lithium",         cap:"Small",  priceRange:[2,20],     volatility:"VHigh"  },
  { ticker:"NXE",  name:"NexGen Energy",       sector:"Materials",  sub:"Uranium",         cap:"Small",  priceRange:[5,12],     volatility:"High"   },
  { ticker:"UEC",  name:"Uranium Energy",      sector:"Materials",  sub:"Uranium",         cap:"Small",  priceRange:[4,12],     volatility:"High"   },
  { ticker:"MP",   name:"MP Materials",        sector:"Materials",  sub:"Rare Earths",     cap:"Mid",    priceRange:[10,30],    volatility:"Med"    },
  { ticker:"AU",   name:"AngloGold Ashanti",   sector:"Materials",  sub:"Gold Miners",     cap:"Mid",    priceRange:[18,36],    volatility:"Med"    },
  { ticker:"RKLB", name:"Rocket Lab USA",      sector:"Space",      sub:"Launch",          cap:"Small",  priceRange:[4,26],     volatility:"High"   },
  { ticker:"ASTS", name:"AST SpaceMobile",     sector:"Space",      sub:"Satellite",       cap:"Small",  priceRange:[5,40],     volatility:"VHigh"  },
  { ticker:"KTOS", name:"Kratos Defence",      sector:"Space",      sub:"Military AI",     cap:"Mid",    priceRange:[15,35],    volatility:"Med"    },
  { ticker:"BEAM", name:"Beam Therapeutics",   sector:"Biomedical", sub:"Gene Therapy",    cap:"Small",  priceRange:[10,40],    volatility:"High"   },
  { ticker:"CRSP", name:"CRISPR Therapeutics", sector:"Biomedical", sub:"Gene Therapy",    cap:"Small",  priceRange:[30,80],    volatility:"High"   },
  { ticker:"COIN", name:"Coinbase",            sector:"Crypto",     sub:"Exchange",        cap:"Mid",    priceRange:[100,330],  volatility:"VHigh"  },
  { ticker:"MSTR", name:"MicroStrategy",       sector:"Crypto",     sub:"BTC Treasury",    cap:"Mid",    priceRange:[100,600],  volatility:"Extreme"},
  { ticker:"HIVE", name:"HIVE Digital",        sector:"Crypto",     sub:"Mining",          cap:"Micro",  priceRange:[2,18],     volatility:"Extreme"},
  { ticker:"GME",  name:"GameStop",            sector:"Consumer",   sub:"Gaming",          cap:"Small",  priceRange:[8,30],     volatility:"Extreme"},
  { ticker:"DKNG", name:"DraftKings",          sector:"Consumer",   sub:"Betting",         cap:"Mid",    priceRange:[15,50],    volatility:"Med"    },
  { ticker:"NFLX", name:"Netflix",             sector:"Consumer",   sub:"Streaming",       cap:"Large",  priceRange:[500,800],  volatility:"Low"    },
  { ticker:"BYND", name:"Beyond Meat",         sector:"Agri",       sub:"Alt Protein",     cap:"Small",  priceRange:[3,20],     volatility:"VHigh"  },
  { ticker:"MOS",  name:"The Mosaic Co",       sector:"Agri",       sub:"Fertilisers",     cap:"Mid",    priceRange:[20,45],    volatility:"Med"    },
  { ticker:"SOFI", name:"SoFi Technologies",   sector:"Finance",    sub:"Fintech",         cap:"Small",  priceRange:[5,22],     volatility:"High"   },
  { ticker:"UPST", name:"Upstart Holdings",    sector:"Finance",    sub:"Fintech",         cap:"Small",  priceRange:[10,80],    volatility:"VHigh"  },
  { ticker:"AFRM", name:"Affirm Holdings",     sector:"Finance",    sub:"Payments",        cap:"Mid",    priceRange:[15,75],    volatility:"High"   },
  { ticker:"EQIX", name:"Equinix REIT",        sector:"Technology", sub:"Data Centres",    cap:"Large",  priceRange:[700,950],  volatility:"Low"    },
];

// ── STOCK API CONFIG ──────────────────────────────────────────
const API_BASE = "https://YOUR_API_URL"; // ← replace with your deployed FastAPI URL
const API_ENABLED = false; // ← set true once your backend is deployed

// ── LIVE PRICE FETCHER (wraps your FastAPI /api/prices endpoint) ──────────────
async function fetchLivePrices(tickers){
  if(!API_ENABLED) return null;
  try{
    const symbols=tickers.join(",");
    const res=await fetch(`${API_BASE}/api/prices?symbols=${symbols}`,{signal:AbortSignal.timeout(8000)});
    if(!res.ok) return null;
    const json=await res.json();
    return json.data||null; // { AAPL: { price, change_pct, volume, ... }, ... }
  }catch(e){
    console.warn("Live price fetch failed, falling back to simulation:",e);
    return null;
  }
}

// ── SIGNAL ENGINE ────────────────────────────────────────────
function sr(seed,min,max){const x=Math.sin(seed+1)*10000;return min+(x-Math.floor(x))*(max-min);}

// Merge live API data into generated signals when available
function mergeLiveData(signals, liveData, asset){
  if(!liveData) return signals;
  const live=liveData[asset.ticker];
  if(!live||!live.price) return signals;
  // Override simulated price with real market price (converted to USD)
  const realPrice=live.currency==="GBX"?live.price/100:live.price; // handle GBX (pence)
  const changeSign=live.change_pct>0?1:live.change_pct<0?-1:0;
  // Nudge RSI based on real price momentum
  const nudgedRsi=Math.max(10,Math.min(90,signals.metrics.rsi+(changeSign*8)));
  // Volume multiplier from real data
  const realVolMult=live.volume&&live.day_high&&live.day_low
    ? Math.min(4,Math.max(0.3,(live.volume/(live.volume+1000000))*3+0.5))
    : signals.metrics.volume;
  return{
    ...signals,
    price:parseFloat(realPrice.toFixed(4)),
    livePrice:true,
    marketState:live.market_state||"CLOSED",
    currency:live.currency||"USD",
    metrics:{...signals.metrics,rsi:nudgedRsi,volume:realVolMult},
  };
}

function generateSignals(asset,key){
  const s=asset.ticker.split("").reduce((a,c)=>a+c.charCodeAt(0),0)+key;
  const r=(min,max,o=0)=>sr(s+o,min,max);
  const capTier=CAP_TIERS[asset.cap]??2;
  const volMod={Low:0.6,Med:0.8,High:1.0,VHigh:1.2,Extreme:1.5}[asset.volatility]||1;
  const rsi=r(18,82,1),macd=r(-1,1,2),volume=r(0.4,4.0,3),sentiment=r(-1,1,4);
  const shortInt=r(0,35,5),earningsBeat=r(-25,40,6),priceVsMA50=r(-30,40,7),priceVsMA200=r(-40,50,8);
  const insiderBuy=r(0,1,9),catalystNews=r(-1,1,10),sectorFlow=r(-1,1,11);
  const daysToEarnings=r(0,90,13),debtRatio=r(0,3,14),revGrowth=r(-20,120,15);
  let sectorScore=0;
  if(asset.sector==="Technology") sectorScore+=sectorFlow>0.3?15:0;
  if(asset.sub==="Uranium"||asset.sub==="Nuclear") sectorScore+=18;
  if(asset.sector==="Space") sectorScore+=12;
  if(asset.sector==="Crypto") sectorScore+=r(0,1,34)>0.5?18:-14;
  if(asset.sector==="Healthcare"&&daysToEarnings<20) sectorScore+=14;
  let score=0;
  score+=rsi<30?22:rsi>72?-18:(50-rsi)*0.3;
  score+=macd*18;score+=(volume-1)*10;score+=sentiment*22;
  score+=shortInt>25?-18:shortInt<5?12:0;
  score+=earningsBeat*0.5;score+=priceVsMA50<-20?14:priceVsMA50>30?-10:0;
  score+=insiderBuy>0.7?18:0;score+=catalystNews*18;score+=sectorFlow*12;
  score+=sectorScore;score+=revGrowth>50?18:revGrowth<-10?-12:0;
  score+=debtRatio>2?-14:0;score+=(4-capTier)*6*r(0.3,1,40);
  score=Math.max(-100,Math.min(100,score));
  const risk=score<-55?"CRITICAL":score<-15?"HIGH":score<18?"MODERATE":score<55?"POSITIVE":"STRONG";
  const price=sr(s+42,asset.priceRange[0],asset.priceRange[1]);
  const stopPct=capTier<=1?0.15:capTier===2?0.10:0.07;
  const maxUpside=capTier===0?400:capTier===1?200:capTier===2?100:capTier===3?50:30;
  const upsidePct=score>0?(score/100)*maxUpside:0;
  const rrRatio=stopPct>0?((upsidePct/100)/stopPct).toFixed(1):"—";
  const entryQ=rsi<35&&priceVsMA50<-10?"IDEAL":rsi<50&&volume>1.2?"GOOD":rsi>68?"POOR":"FAIR";
  const tfScores={};
  const intRaw=(Math.max(0,(volume-1.5)*35)+(shortInt>20&&volume>2?25:0)+(rsi>28&&rsi<52?12:0)+(catalystNews>0.5?20:0)+(sentiment>0.5?10:0)+(macd>0.3?8:0)-(rsi>70?15:0))*volMod;
  tfScores["⚡ Intraday"]=Math.max(5,Math.min(95,intRaw));
  const swRaw=(macd>0?22:0)+(rsi<45&&rsi>25?20:0)+(volume>1.3?15:0)+(earningsBeat>10?18:0)+(priceVsMA50<-10?12:0)+(sentiment>0?8:0)+(catalystNews>0?10:0)-(debtRatio>2?10:0);
  tfScores["📈 Short Swing"]=Math.max(5,Math.min(95,swRaw));
  const medRaw=(sectorFlow>0.2?22:0)+(revGrowth>20?20:0)+(macd>0?14:0)+(volume>1.1?10:0)+(priceVsMA200<-15?15:0)+(insiderBuy>0.5?12:0)+(daysToEarnings<30?14:0)-(debtRatio>2?12:0);
  tfScores["🌊 Medium Swing"]=Math.max(5,Math.min(95,medRaw));
  const posRaw=(revGrowth>30?28:revGrowth>10?16:0)+(insiderBuy>0.65?22:0)+(debtRatio<1?18:debtRatio<2?8:0)+(sectorFlow>0.3?15:0)+(sentiment>0.3?12:0)+(capTier>=2?10:0);
  tfScores["🏔️ Position"]=Math.max(5,Math.min(95,posRaw));
  const ltRaw=(revGrowth>20?25:0)+(debtRatio<1.5?20:0)+(capTier>=3?22:capTier===2?12:0)+(insiderBuy>0.5?15:0)+(sectorFlow>0?10:0)+(sentiment>0?8:0);
  tfScores["🌳 Long Term"]=Math.max(5,Math.min(95,ltRaw));
  const bestTF=Object.entries(tfScores).sort((a,b)=>b[1]-a[1])[0][0];
  return{score:Math.round(score),risk,price:parseFloat(price.toFixed(2)),upsidePct:parseFloat(upsidePct.toFixed(1)),stopPct:parseFloat((stopPct*100).toFixed(0)),rrRatio,entryQ,tfScores,bestTF,livePrice:false,metrics:{rsi,macd,volume,sentiment,shortInt,revGrowth,debtRatio,daysToEarnings}};
}

function simulateTradeProgress(trade,signals){
  const asset=ASSETS.find(a=>a.ticker===trade.ticker);
  if(!asset) return{currentPrice:trade.entryPrice,pctChange:0,status:"open"};
  const volFactor={Low:0.003,Med:0.006,High:0.012,VHigh:0.018,Extreme:0.025}[asset.volatility]||0.01;
  const simDays=TF_PROFILES[trade.timeframe]?.simDays||7;
  const tradeSeed=trade.id*7919;
  const bullBias=(trade.signalScore/100)*0.6,tfBias=(trade.tfScore/100-0.5)*0.4;
  let price=trade.entryPrice;
  const steps=Math.max(1,Math.round(simDays));
  for(let i=0;i<steps;i++){const rand=sr(tradeSeed+i*17,-1,1);price=price*(1+(bullBias+tfBias)*volFactor*0.5+rand*volFactor);}
  price=Math.max(price*0.01,price);
  const pctChange=(price-trade.entryPrice)/trade.entryPrice*100;
  let status="open";
  if(pctChange<=-trade.stopLossPct){price=trade.entryPrice*(1-trade.stopLossPct/100);status="stopped";}
  else if(pctChange>=trade.targetPct){price=trade.entryPrice*(1+trade.targetPct/100);status="target_hit";}
  return{currentPrice:parseFloat(price.toFixed(2)),pctChange:parseFloat(pctChange.toFixed(2)),status};
}

function scoreWatchlistPrediction(item,currentSignals){
  if(!currentSignals) return null;
  const orig=item.signalAtAdd;
  const predictedBull=orig.score>20,predictedBear=orig.score<-20;
  const actualMove=item.simulatedFinalPrice?((item.simulatedFinalPrice-item.priceAtAdd)/item.priceAtAdd)*100:0;
  const actualBull=actualMove>2,actualBear=actualMove<-2;
  const directionCorrect=(predictedBull&&actualBull)||(predictedBear&&actualBear)||(!predictedBull&&!predictedBear&&Math.abs(actualMove)<5);
  const expectedMove=orig.upsidePct;
  const accuracy=Math.max(0,100-Math.abs(actualMove-expectedMove)*2);
  const grade=directionCorrect&&accuracy>70?"A":directionCorrect&&accuracy>40?"B":!directionCorrect&&Math.abs(actualMove)>15?"D":"C";
  const gradeColor=grade==="A"?"#00e676":grade==="B"?"#00d4ff":grade==="C"?"#ffd600":"#ff1744";
  const gradeLabel=grade==="A"?"Prediction aligned well":grade==="B"?"Partial alignment":grade==="C"?"Mixed result":"Prediction off-target";
  return{directionCorrect,actualMove:parseFloat(actualMove.toFixed(2)),expectedMove:parseFloat(expectedMove.toFixed(1)),accuracy:parseFloat(accuracy.toFixed(0)),grade,gradeColor,gradeLabel,tfScore:orig.tfScores[item.watchTimeframe]};
}

function calculateAccuracy(trades){
  const closed=trades.filter(t=>t.status==="closed"||t.status==="stopped"||t.status==="target_hit");
  if(closed.length===0) return{overall:null,byTF:{},wins:0,losses:0,avgReturn:0,highProbWinRate:null,lowProbWinRate:null,signalAccuracy:[],totalClosed:0};
  const wins=closed.filter(t=>t.finalPnLPct>0).length;
  const losses=closed.filter(t=>t.finalPnLPct<=0).length;
  const overall=(wins/closed.length)*100;
  const byTF={};
  TF_KEYS.forEach(tf=>{const tft=closed.filter(t=>t.timeframe===tf);if(tft.length>0){const tw=tft.filter(t=>t.finalPnLPct>0).length;byTF[tf]={wins:tw,total:tft.length,rate:(tw/tft.length)*100};}});
  const hp=closed.filter(t=>t.tfScore>=60),lp=closed.filter(t=>t.tfScore<60);
  const highProbWinRate=hp.length>0?(hp.filter(t=>t.finalPnLPct>0).length/hp.length)*100:null;
  const lowProbWinRate=lp.length>0?(lp.filter(t=>t.finalPnLPct>0).length/lp.length)*100:null;
  const avgReturn=closed.reduce((a,t)=>a+t.finalPnLPct,0)/closed.length;
  const signalAccuracy=["STRONG","POSITIVE","MODERATE","HIGH","CRITICAL"].map(risk=>{const rt=closed.filter(t=>t.signalRisk===risk);return{risk,wins:rt.filter(t=>t.finalPnLPct>0).length,total:rt.length,rate:rt.length>0?(rt.filter(t=>t.finalPnLPct>0).length/rt.length)*100:null};});
  return{overall,byTF,wins,losses,avgReturn,highProbWinRate,lowProbWinRate,signalAccuracy,totalClosed:closed.length};
}

// ── HELPERS ──────────────────────────────────────────────────
const fmtPct=(n,d=1)=>n===undefined||n===null?"—":`${n>0?"+":""}${n.toFixed(d)}%`;
const pnlColor=v=>v>0?"#00e676":v<0?"#ff1744":"#888";
const ratingColor=r=>r>=75?"#00e676":r>=60?"#ffd600":r>=45?"#ff9800":"#ff1744";
const ratingLabel=r=>r>=75?"🟢 STRONG":r>=60?"🟡 GOOD":r>=45?"🟠 FAIR":"🔴 WEAK";
const Tag=({children,color="#888"})=>(<span style={{fontSize:8,color,border:`1px solid ${color}44`,borderRadius:3,padding:"1px 5px",fontFamily:"monospace",letterSpacing:1}}>{children}</span>);
const Pill=({label,active,color,onClick})=>(<button onClick={onClick} style={{background:active?"#12121e":"transparent",border:`1px solid ${active?(color||"#4a4aaa"):"#12121e"}`,borderRadius:20,padding:"3px 10px",cursor:"pointer",color:active?(color||"#c0c0ff"):"#2a2a45",fontSize:9,fontFamily:"monospace",whiteSpace:"nowrap"}}>{label}</button>);

// ── CURRENCY MODAL ───────────────────────────────────────────
function CurrencyModal({current,onSelect,onClose}){
  const [search,setSearch]=useState("");
  const filtered=CURRENCIES.filter(c=>c.code.toLowerCase().includes(search.toLowerCase())||c.name.toLowerCase().includes(search.toLowerCase()));
  return(
    <div style={{position:"fixed",inset:0,background:"#000000cc",zIndex:400,display:"flex",alignItems:"center",justifyContent:"center",padding:20}} onClick={onClose}>
      <div onClick={e=>e.stopPropagation()} style={{background:"#07070f",border:"1px solid #00b0ff",borderRadius:12,maxWidth:380,width:"100%",maxHeight:"80vh",overflow:"hidden",display:"flex",flexDirection:"column",boxShadow:"0 0 60px #00b0ff22"}}>
        <div style={{background:"#0a0a16",borderBottom:"1px solid #00b0ff33",padding:"14px 16px",borderRadius:"12px 12px 0 0"}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:10}}>
            <div>
              <div style={{fontSize:8,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2}}>ACCOUNT CURRENCY</div>
              <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:17,fontWeight:700,color:"#dde0ff"}}>Select Currency</div>
            </div>
            <button onClick={onClose} style={{background:"none",border:"none",color:"#444",cursor:"pointer",fontSize:16}}>✕</button>
          </div>
          <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search currency..." style={{width:"100%",background:"#0e0e1c",border:"1px solid #1a1a2e",borderRadius:6,padding:"7px 10px",color:"#c0c0e0",fontFamily:"monospace",fontSize:11,outline:"none",boxSizing:"border-box"}}/>
        </div>
        <div style={{overflowY:"auto",padding:8}}>
          {filtered.map(c=>{
            const isActive=c.code===current.code;
            return(
              <div key={c.code+c.name} onClick={()=>{onSelect(c);onClose();}} style={{display:"flex",alignItems:"center",gap:12,padding:"10px 12px",borderRadius:7,cursor:"pointer",background:isActive?"#00b0ff18":"transparent",border:`1px solid ${isActive?"#00b0ff44":"transparent"}`,marginBottom:3}}
                onMouseEnter={e=>{if(!isActive)e.currentTarget.style.background="#0e0e18";}} onMouseLeave={e=>{if(!isActive)e.currentTarget.style.background="transparent";}}>
                <span style={{fontSize:20,flexShrink:0}}>{c.flag}</span>
                <div style={{flex:1}}>
                  <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                    <span style={{fontFamily:"monospace",fontSize:12,fontWeight:700,color:isActive?"#00b0ff":"#dde0ff"}}>{c.code}</span>
                    <span style={{fontFamily:"monospace",fontSize:14,fontWeight:700,color:isActive?"#00b0ff":"#8a8aaa"}}>{c.symbol}</span>
                  </div>
                  <div style={{fontSize:9,color:"#4a4a6a"}}>{c.name}</div>
                </div>
                <div style={{textAlign:"right",flexShrink:0}}>
                  <div style={{fontSize:8,color:"#2a2a40",fontFamily:"monospace"}}>vs USD</div>
                  <div style={{fontSize:10,color:"#6a6a8a",fontFamily:"monospace"}}>{(c.rate/1.27).toFixed(c.rate>10?1:3)}</div>
                </div>
                {isActive&&<span style={{color:"#00b0ff",fontSize:14}}>✓</span>}
              </div>
            );
          })}
        </div>
        <div style={{padding:"10px 14px",borderTop:"1px solid #0e0e18",fontSize:8,color:"#2a2a40",fontFamily:"monospace",textAlign:"center",lineHeight:1.6}}>
          Rates indicative only. All prices converted from USD at fixed simulation rate.
        </div>
      </div>
    </div>
  );
}

// ── ADVICE CARD ───────────────────────────────────────────────
function AdviceCard({card,onDismiss,compact}){
  if(!card) return null;
  if(compact) return(
    <div style={{background:card.color+"15",border:`1px solid ${card.color}44`,borderRadius:7,padding:"10px 12px",marginBottom:8,position:"relative"}}>
      <div style={{position:"absolute",top:0,left:0,bottom:0,width:3,background:card.color,borderRadius:"7px 0 0 7px"}}/>
      <div style={{paddingLeft:8}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:4}}>
          <div style={{display:"flex",gap:6,alignItems:"center"}}><span style={{fontSize:14}}>{card.icon}</span><span style={{fontSize:8,color:card.color,fontFamily:"monospace",letterSpacing:1.5}}>{card.tag}</span></div>
          {onDismiss&&<button onClick={onDismiss} style={{background:"none",border:"none",color:"#333",cursor:"pointer",fontSize:12,padding:0,lineHeight:1}}>×</button>}
        </div>
        <div style={{fontSize:11,color:"#c0c0dd",fontWeight:600,marginBottom:4,lineHeight:1.3}}>{card.title}</div>
        <div style={{fontSize:10,color:"#7a7a9a",lineHeight:1.6}}>{card.body}</div>
      </div>
    </div>
  );
  return(
    <div style={{position:"fixed",inset:0,background:"#000000cc",zIndex:400,display:"flex",alignItems:"center",justifyContent:"center",padding:20}} onClick={onDismiss}>
      <div onClick={e=>e.stopPropagation()} style={{background:"#07070f",border:`1px solid ${card.color}`,borderRadius:12,padding:28,maxWidth:400,width:"100%",boxShadow:`0 0 80px ${card.color}33`,position:"relative"}}>
        <div style={{height:3,background:card.color,borderRadius:"12px 12px 0 0",position:"absolute",top:0,left:0,right:0}}/>
        <div style={{fontSize:9,color:card.color,fontFamily:"monospace",letterSpacing:2,marginBottom:8}}>{card.tag}</div>
        <div style={{fontSize:30,marginBottom:10}}>{card.icon}</div>
        <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:20,fontWeight:700,color:"#dde0ff",marginBottom:14,lineHeight:1.2}}>{card.title}</div>
        <div style={{fontSize:13,color:"#8a8aaa",lineHeight:1.8}}>{card.body}</div>
        <button onClick={onDismiss} style={{marginTop:20,width:"100%",background:`${card.color}22`,border:`1px solid ${card.color}`,borderRadius:6,padding:"10px",color:card.color,fontFamily:"monospace",fontSize:11,cursor:"pointer",letterSpacing:2}}>GOT IT ✓</button>
      </div>
    </div>
  );
}

// ── PLACE TRADE MODAL ─────────────────────────────────────────
function PlaceTradeModal({asset,signals,balance,currency,toLocal,fmtMoney,onPlace,onClose}){
  const R=RISK_CFG[signals.risk];
  const [step,setStep]=useState(0);
  const [tf,setTF]=useState(signals.bestTF);
  const [shares,setShares]=useState("");
  const [customStop,setCustomStop]=useState(signals.stopPct.toString());
  const [customTarget,setCustomTarget]=useState(signals.upsidePct.toFixed(1));
  const [dismissedAdvice,setDismissedAdvice]=useState({});

  // All money in local currency
  const localPrice=toLocal(signals.price);
  const sharesN=parseFloat(shares)||0;
  const stopPctN=parseFloat(customStop)||signals.stopPct;
  const targetPctN=parseFloat(customTarget)||signals.upsidePct;
  const totalCost=sharesN*localPrice;
  const maxLoss=totalCost*(stopPctN/100);
  const maxGain=totalCost*(targetPctN/100);
  const rrRatio=stopPctN>0?(targetPctN/stopPctN).toFixed(1):"—";
  const pctBalance=balance>0?(totalCost/balance)*100:0;
  const risk2pct=balance*0.02;
  const suggestedShares=stopPctN>0&&localPrice>0?Math.floor(risk2pct/(localPrice*stopPctN/100)):0;
  const tfProf=TF_PROFILES[tf];
  const tfScore=signals.tfScores[tf];
  const rrOk=parseFloat(rrRatio)>=2;
  const rrC=rrOk?"#00e676":parseFloat(rrRatio)>=1?"#ffd600":"#ff1744";
  const canPlace=sharesN>0&&totalCost<=balance;
  const steps=["TIMEFRAME","SIZING","RISK/REWARD","ENTRY","CONFIRM"];
  const sym=currency.symbol;

  return(
    <div style={{position:"fixed",inset:0,background:"#000000dd",zIndex:300,display:"flex",alignItems:"center",justifyContent:"center",padding:12}}>
      <div style={{background:"#06060f",border:`1px solid ${R.color}`,borderRadius:12,maxWidth:520,width:"100%",maxHeight:"94vh",overflowY:"auto",boxShadow:`0 0 80px ${R.color}22`,display:"flex",flexDirection:"column"}}>
        <div style={{background:"#0a0a16",borderBottom:`1px solid ${R.color}44`,padding:"12px 16px",borderRadius:"12px 12px 0 0",position:"sticky",top:0,zIndex:10}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:10}}>
            <div>
              <div style={{fontSize:8,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2}}>VIRTUAL TRADE ENTRY</div>
              <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:18,fontWeight:700,color:"#dde0ff"}}>{asset.ticker} <span style={{color:"#5a5a7a",fontSize:13}}>{asset.name}</span></div>
            </div>
            <div style={{display:"flex",gap:8,alignItems:"center"}}>
              <div style={{textAlign:"right"}}>
                <div style={{fontFamily:"monospace",fontSize:15,fontWeight:700,color:"#dde0ff"}}>{fmtMoney(signals.price)}</div>
                <Tag color={R.color}>{R.label}</Tag>
              </div>
              <button onClick={onClose} style={{background:"none",border:"none",color:"#444",cursor:"pointer",fontSize:16}}>✕</button>
            </div>
          </div>
          <div style={{display:"flex",gap:3}}>
            {steps.map((s,i)=>(
              <div key={i} onClick={()=>setStep(i)} style={{flex:1,cursor:"pointer"}}>
                <div style={{fontSize:7,color:step===i?R.color:"#2a2a40",fontFamily:"monospace",textAlign:"center",marginBottom:3,letterSpacing:0.5}}>{s}</div>
                <div style={{height:3,background:i<=step?R.color:"#1a1a2e",borderRadius:2}}/>
              </div>
            ))}
          </div>
        </div>
        <div style={{padding:"14px 16px",flex:1}}>
          {step===0&&(
            <div>
              <div style={{fontSize:9,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:10}}>SELECT YOUR TIMEFRAME THESIS</div>
              {TRADE_ADVICE.timeframe.map((card,i)=>(!dismissedAdvice[`tf_${i}`]&&<AdviceCard key={i} card={card} compact onDismiss={()=>setDismissedAdvice(p=>({...p,[`tf_${i}`]:true}))}/>))}
              <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:6,marginBottom:14}}>
                {TF_KEYS.map(k=>{
                  const p=TF_PROFILES[k],sc=signals.tfScores[k];
                  const pc=sc>=72?"#00e676":sc>=52?"#00d4ff":sc>=35?"#ffd600":"#ff6d00";
                  const isActive=tf===k;
                  return(<div key={k} onClick={()=>setTF(k)} style={{background:isActive?p.color+"22":"#0a0a16",border:`1px solid ${isActive?p.color:"#1a1a2e"}`,borderRadius:7,padding:"10px 11px",cursor:"pointer"}}>
                    <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:3}}>
                      <span style={{fontSize:11,color:isActive?p.color:"#5a5a7a",fontFamily:"monospace",fontWeight:700}}>{p.icon} {p.short}</span>
                      <span style={{fontSize:11,color:pc,fontFamily:"monospace",fontWeight:700}}>{sc.toFixed(0)}%</span>
                    </div>
                    <div style={{fontSize:8,color:isActive?"#8a8aaa":"#3a3a50",marginBottom:2}}>{p.traderType}</div>
                    <div style={{fontSize:8,color:"#2a2a40"}}>{p.timeCommit}</div>
                    {isActive&&<div style={{marginTop:6,height:2,background:p.color,borderRadius:1}}/>}
                  </div>);
                })}
              </div>
              <button onClick={()=>setStep(1)} style={{width:"100%",background:tfProf.color+"22",border:`1px solid ${tfProf.color}`,borderRadius:6,padding:"10px",color:tfProf.color,fontFamily:"monospace",fontSize:10,cursor:"pointer",letterSpacing:2}}>NEXT: POSITION SIZING →</button>
            </div>
          )}
          {step===1&&(
            <div>
              <div style={{fontSize:9,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:10}}>POSITION SIZING</div>
              {TRADE_ADVICE.sizing.map((card,i)=>(!dismissedAdvice[`sz_${i}`]&&<AdviceCard key={i} card={card} compact onDismiss={()=>setDismissedAdvice(p=>({...p,[`sz_${i}`]:true}))}/>))}
              <div style={{background:"#0a0a16",borderRadius:7,padding:12,marginBottom:12}}>
                {[["Available Cash",`${sym}${balance.toFixed(balance>100?0:2)}`,"#c0c0e0"],["2% Risk Rule Max",`${sym}${risk2pct.toFixed(risk2pct>100?0:2)}`,"#00e676"],["Suggested Shares",`${suggestedShares} shares`,"#00b0ff"],["Cost at Suggestion",`${sym}${(suggestedShares*localPrice).toFixed(localPrice>100?0:2)}`,"#c0c0e0"]].map(([l,v,c])=>(
                  <div key={l} style={{display:"flex",justifyContent:"space-between",padding:"5px 0",borderBottom:"1px solid #0e0e18",fontSize:11}}>
                    <span style={{color:"#3a3a55",fontFamily:"monospace"}}>{l}</span>
                    <span style={{color:c,fontFamily:"monospace",fontWeight:700}}>{v}</span>
                  </div>
                ))}
              </div>
              <div style={{marginBottom:10}}>
                <div style={{display:"flex",justifyContent:"space-between",marginBottom:4}}>
                  <span style={{fontSize:8,color:"#2a2a40",fontFamily:"monospace",letterSpacing:1.5}}>NUMBER OF SHARES</span>
                  <button onClick={()=>setShares(suggestedShares.toString())} style={{fontSize:8,color:"#00b0ff",background:"none",border:"none",cursor:"pointer",fontFamily:"monospace"}}>← USE 2% RULE</button>
                </div>
                <input value={shares} onChange={e=>setShares(e.target.value)} placeholder={`Suggested: ${suggestedShares}`} style={{width:"100%",background:"#0e0e1c",border:"1px solid #1a1a2e",borderRadius:5,padding:"7px 10px",color:"#c0c0e0",fontFamily:"monospace",fontSize:13,outline:"none",boxSizing:"border-box"}}/>
              </div>
              {sharesN>0&&(<div style={{background:pctBalance>30?"#160900":"#0a0a16",border:`1px solid ${pctBalance>30?"#ff6d0044":"#1a1a2e"}`,borderRadius:6,padding:10,marginBottom:12,fontSize:10}}>
                <div style={{display:"flex",justifyContent:"space-between"}}><span style={{color:"#5a5a7a"}}>Total cost</span><span style={{color:"#c0c0e0",fontFamily:"monospace"}}>{sym}{totalCost.toFixed(totalCost>100?0:2)}</span></div>
                <div style={{display:"flex",justifyContent:"space-between"}}><span style={{color:"#5a5a7a"}}>% of account</span><span style={{color:pctBalance>30?"#ff6d00":"#c0c0e0",fontFamily:"monospace"}}>{pctBalance.toFixed(1)}%{pctBalance>30?" ⚠ CONCENTRATED":""}</span></div>
              </div>)}
              <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
                <button onClick={()=>setStep(0)} style={{background:"transparent",border:"1px solid #2a2a40",borderRadius:6,padding:"9px",color:"#4a4a6a",fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>← BACK</button>
                <button onClick={()=>setStep(2)} style={{background:"#00b0ff22",border:"1px solid #00b0ff",borderRadius:6,padding:"9px",color:"#00b0ff",fontFamily:"monospace",fontSize:9,cursor:"pointer",letterSpacing:1}}>NEXT: R/R →</button>
              </div>
            </div>
          )}
          {step===2&&(
            <div>
              <div style={{fontSize:9,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:10}}>RISK / REWARD PARAMETERS</div>
              {TRADE_ADVICE.rr.map((card,i)=>(!dismissedAdvice[`rr_${i}`]&&<AdviceCard key={i} card={card} compact onDismiss={()=>setDismissedAdvice(p=>({...p,[`rr_${i}`]:true}))}/>))}
              <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginBottom:12}}>
                {[["Stop Loss %",customStop,setCustomStop,`Suggested: ${signals.stopPct}%`],["Target %",customTarget,setCustomTarget,`Suggested: ${signals.upsidePct.toFixed(1)}%`]].map(([l,v,set,hint])=>(
                  <div key={l}><div style={{fontSize:8,color:"#2a2a40",fontFamily:"monospace",letterSpacing:1.5,marginBottom:3}}>{l}</div>
                  <input value={v} onChange={e=>set(e.target.value)} style={{width:"100%",background:"#0e0e1c",border:"1px solid #1a1a2e",borderRadius:5,padding:"6px 8px",color:"#c0c0e0",fontFamily:"monospace",fontSize:12,outline:"none",boxSizing:"border-box"}}/>
                  <div style={{fontSize:7,color:"#2a2a35",marginTop:2}}>{hint}</div></div>
                ))}
              </div>
              <div style={{background:rrOk?"#001a0a":"#160004",border:`1px solid ${rrC}44`,borderRadius:7,padding:"12px 14px",marginBottom:12,display:"flex",gap:12,alignItems:"center"}}>
                <span style={{fontSize:24}}>{rrOk?"✅":"⚠️"}</span>
                <div>
                  <div style={{fontSize:13,color:rrC,fontFamily:"monospace",fontWeight:700}}>R/R RATIO: {rrRatio}:1</div>
                  <div style={{fontSize:10,color:"#5a5a7a",marginTop:2}}>{rrOk?"Minimum 2:1 met ✓":"Below 2:1 — adjust stop or target"}</div>
                </div>
              </div>
              {sharesN>0&&(<div style={{background:"#0a0a16",borderRadius:6,padding:10,marginBottom:12}}>
                <div style={{display:"flex",justifyContent:"space-between",padding:"4px 0",fontSize:10}}><span style={{color:"#3a3a55"}}>Max loss at stop</span><span style={{color:"#ff1744",fontFamily:"monospace",fontWeight:700}}>-{sym}{maxLoss.toFixed(maxLoss>100?0:2)}</span></div>
                <div style={{display:"flex",justifyContent:"space-between",padding:"4px 0",fontSize:10}}><span style={{color:"#3a3a55"}}>Max gain at target</span><span style={{color:"#00e676",fontFamily:"monospace",fontWeight:700}}>+{sym}{maxGain.toFixed(maxGain>100?0:2)}</span></div>
              </div>)}
              <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
                <button onClick={()=>setStep(1)} style={{background:"transparent",border:"1px solid #2a2a40",borderRadius:6,padding:"9px",color:"#4a4a6a",fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>← BACK</button>
                <button onClick={()=>setStep(3)} style={{background:"#00e67622",border:"1px solid #00e676",borderRadius:6,padding:"9px",color:"#00e676",fontFamily:"monospace",fontSize:9,cursor:"pointer",letterSpacing:1}}>NEXT: ENTRY →</button>
              </div>
            </div>
          )}
          {step===3&&(
            <div>
              <div style={{fontSize:9,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:10}}>ENTRY QUALITY CHECK</div>
              {TRADE_ADVICE.entry.map((card,i)=>(!dismissedAdvice[`en_${i}`]&&<AdviceCard key={i} card={card} compact onDismiss={()=>setDismissedAdvice(p=>({...p,[`en_${i}`]:true}))}/>))}
              <div style={{background:"#0a0a16",borderRadius:7,padding:12,marginBottom:12}}>
                {[{label:"Entry Quality",val:signals.entryQ,good:signals.entryQ==="IDEAL"||signals.entryQ==="GOOD"},{label:"RSI",val:signals.metrics.rsi.toFixed(0),good:signals.metrics.rsi<50&&signals.metrics.rsi>25},{label:"Volume ×",val:signals.metrics.volume.toFixed(1)+"×",good:signals.metrics.volume>1.2},{label:"MACD",val:(signals.metrics.macd>0?"+":"")+signals.metrics.macd.toFixed(2),good:signals.metrics.macd>0},{label:"Sentiment",val:signals.metrics.sentiment.toFixed(2),good:signals.metrics.sentiment>0},{label:"Days to Event",val:signals.metrics.daysToEarnings.toFixed(0)+"d",good:signals.metrics.daysToEarnings>15}].map(m=>(
                  <div key={m.label} style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"6px 0",borderBottom:"1px solid #0e0e18"}}>
                    <span style={{fontSize:10,color:"#5a5a7a",fontFamily:"monospace"}}>{m.label}</span>
                    <div style={{display:"flex",gap:6,alignItems:"center"}}>
                      <span style={{fontSize:10,color:m.good?"#00e676":"#ff6d00",fontFamily:"monospace",fontWeight:700}}>{m.val}</span>
                      <span style={{color:m.good?"#00e676":"#ff6d00",fontSize:11}}>{m.good?"✓":"!"}</span>
                    </div>
                  </div>
                ))}
              </div>
              <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
                <button onClick={()=>setStep(2)} style={{background:"transparent",border:"1px solid #2a2a40",borderRadius:6,padding:"9px",color:"#4a4a6a",fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>← BACK</button>
                <button onClick={()=>setStep(4)} style={{background:"#ffd60022",border:"1px solid #ffd600",borderRadius:6,padding:"9px",color:"#ffd600",fontFamily:"monospace",fontSize:9,cursor:"pointer",letterSpacing:1}}>NEXT: CONFIRM →</button>
              </div>
            </div>
          )}
          {step===4&&(
            <div>
              <div style={{fontSize:9,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:10}}>FINAL CHECKLIST & CONFIRM</div>
              {TRADE_ADVICE.checklist.map((card,i)=>(!dismissedAdvice[`ck_${i}`]&&<AdviceCard key={i} card={card} compact onDismiss={()=>setDismissedAdvice(p=>({...p,[`ck_${i}`]:true}))}/>))}
              <div style={{background:"#0a0a16",borderRadius:7,padding:12,marginBottom:12}}>
                {[["Timeframe",`${tfProf.icon} ${tfProf.short}`,tfProf.color],["Signal Align",`${tfScore.toFixed(0)}%`,tfProf.color],["Shares",sharesN||"—","#c0c0e0"],["Total Cost",`${sym}${totalCost.toFixed(totalCost>100?0:2)}`,pctBalance>30?"#ff6d00":"#c0c0e0"],["R/R",`${rrRatio}:1`,rrOk?"#00e676":"#ff6d00"],["Max Loss",`-${sym}${maxLoss.toFixed(maxLoss>100?0:2)}`,"#ff1744"],["Max Gain",`+${sym}${maxGain.toFixed(maxGain>100?0:2)}`,"#00e676"],["Entry Q",signals.entryQ,signals.entryQ==="IDEAL"?"#00e676":signals.entryQ==="GOOD"?"#00b0ff":"#ffd600"]].map(([l,v,c])=>(
                  <div key={l} style={{display:"flex",justifyContent:"space-between",padding:"4px 0",borderBottom:"1px solid #0e0e18",fontSize:11}}>
                    <span style={{color:"#3a3a55",fontFamily:"monospace"}}>{l}</span>
                    <span style={{color:c,fontFamily:"monospace",fontWeight:700}}>{v}</span>
                  </div>
                ))}
              </div>
              <div style={{marginBottom:14}}>
                {[[rrOk,"R/R ≥ 2:1"],[pctBalance<=50||pctBalance===0,"Position ≤ 50% of account"],[parseFloat(maxLoss)<=balance*0.05||sharesN===0,"Max loss ≤ 5% of account"],[totalCost<=balance,"Sufficient funds"],[tfScore>=35,"Timeframe ≥ LOW signal alignment"]].map(([ok,t],i)=>(
                  <div key={i} style={{display:"flex",gap:8,padding:"4px 0",fontSize:10,color:"#6a6a8a"}}><span style={{color:ok?"#00e676":"#ff6d00",flexShrink:0}}>{ok?"✓":"✗"}</span><span>{t}</span></div>
                ))}
              </div>
              <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginBottom:8}}>
                <button onClick={()=>setStep(3)} style={{background:"transparent",border:"1px solid #2a2a40",borderRadius:6,padding:"10px",color:"#4a4a6a",fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>← BACK</button>
                <button onClick={()=>{
                  if(!canPlace) return;
                  onPlace({ticker:asset.ticker,name:asset.name,timeframe:tf,entryPrice:localPrice,shares:sharesN,totalCost:parseFloat(totalCost.toFixed(2)),stopLossPct:parseFloat(stopPctN.toFixed(1)),targetPct:parseFloat(targetPctN.toFixed(1)),tfScore:parseFloat(tfScore.toFixed(1)),signalScore:signals.score,signalRisk:signals.risk,rrRatio:parseFloat(rrRatio)||0,entryQ:signals.entryQ});
                  onClose();
                }} disabled={!canPlace}
                  style={{background:canPlace?R.color+"22":"#1a1a2e",border:`1px solid ${canPlace?R.color:"#2a2a40"}`,borderRadius:6,padding:"10px",color:canPlace?R.color:"#3a3a50",fontFamily:"monospace",fontSize:9,cursor:canPlace?"pointer":"not-allowed",letterSpacing:1}}>
                  ✓ CONFIRM VIRTUAL TRADE
                </button>
              </div>
              <div style={{fontSize:8,color:"#1e1e2e",fontFamily:"monospace",textAlign:"center"}}>⚠ Simulation only. Not financial advice.</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── WATCHLIST MODAL ───────────────────────────────────────────
function WatchlistModal({asset,signals,currency,toLocal,fmtMoney,onAdd,onClose}){
  const [selectedDur,setSelectedDur]=useState(WATCH_DURATIONS[0]);
  const [activeGroup,setActiveGroup]=useState("Intraday");
  const watchTF=selectedDur.tf;
  const tfProf=TF_PROFILES[watchTF];
  const tfScore=signals.tfScores[watchTF];
  const predictionSeed=asset.ticker.split("").reduce((a,c)=>a+c.charCodeAt(0),0);
  const bullBias=(signals.score/100)*0.6,tfBias=(tfScore/100-0.5)*0.4;
  const asset2=ASSETS.find(a=>a.ticker===asset.ticker);
  const volFactor={Low:0.003,Med:0.006,High:0.012,VHigh:0.018,Extreme:0.025}[asset2?.volatility]||0.01;
  const simSteps=Math.max(1,Math.round(selectedDur.simDays));
  let simPrice=signals.price; // USD
  for(let i=0;i<simSteps;i++){const rand=sr(predictionSeed+i*23,-1,1);simPrice=simPrice*(1+(bullBias+tfBias)*volFactor*0.5+rand*volFactor);}
  const predictedChange=((simPrice-signals.price)/signals.price)*100;
  const predColor=predictedChange>0?"#00e676":"#ff1744";
  const groupDurations=WATCH_DURATIONS.filter(d=>d.group===activeGroup);
  const expiryDate=new Date(Date.now()+selectedDur.realMs);
  const expiryStr=expiryDate.toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"});
  const localPrice=toLocal(signals.price);
  const localSimPrice=toLocal(simPrice);
  const sym=currency.symbol;

  return(
    <div style={{position:"fixed",inset:0,background:"#000000cc",zIndex:300,display:"flex",alignItems:"center",justifyContent:"center",padding:16}}>
      <div style={{background:"#07070f",border:"1px solid #ffd600",borderRadius:12,maxWidth:460,width:"100%",maxHeight:"92vh",overflowY:"auto",boxShadow:"0 0 60px #ffd60022"}}>
        <div style={{background:"#0a0a14",borderBottom:"1px solid #ffd60033",padding:"13px 16px",borderRadius:"12px 12px 0 0",position:"sticky",top:0,zIndex:10}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
            <div>
              <div style={{fontSize:8,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2}}>★ SET WATCH PERIOD</div>
              <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:18,fontWeight:700,color:"#dde0ff"}}>{asset.ticker} <span style={{color:"#5a5a7a",fontSize:13}}>{asset.name}</span></div>
            </div>
            <button onClick={onClose} style={{background:"none",border:"none",color:"#444",cursor:"pointer",fontSize:16}}>✕</button>
          </div>
        </div>
        <div style={{padding:"14px 16px"}}>
          <div style={{fontSize:9,color:"#ffd600",fontFamily:"monospace",letterSpacing:2,marginBottom:4}}>HOW IT WORKS</div>
          <div style={{fontSize:10,color:"#5a5a7a",lineHeight:1.7,marginBottom:14}}>Pick a real calendar duration. The engine records today's signals and projects the predicted move. On the expiry date it self-grades comparing predicted direction vs simulated outcome.</div>
          <div style={{fontSize:9,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:8}}>SELECT DURATION</div>
          <div style={{display:"flex",gap:5,marginBottom:10,flexWrap:"wrap"}}>
            {WATCH_GROUPS.map(g=>(
              <button key={g} onClick={()=>{setActiveGroup(g);setSelectedDur(WATCH_DURATIONS.find(d=>d.group===g));}} style={{background:activeGroup===g?GROUP_COLORS[g]+"22":"transparent",border:`1px solid ${activeGroup===g?GROUP_COLORS[g]:"#1a1a2e"}`,borderRadius:20,padding:"4px 12px",cursor:"pointer",color:activeGroup===g?GROUP_COLORS[g]:"#3a3a55",fontSize:9,fontFamily:"monospace",letterSpacing:1}}>
                {g==="Intraday"?"⚡ INTRADAY":g==="Short"?"📈 DAYS":g==="Swing"?"🌊 WEEKS":"🏔️ MONTHS"}
              </button>
            ))}
          </div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(90px,1fr))",gap:6,marginBottom:14}}>
            {groupDurations.map(d=>{
              const isActive=selectedDur.label===d.label,gc=GROUP_COLORS[d.group];
              return(<div key={d.label} onClick={()=>setSelectedDur(d)} style={{background:isActive?gc+"22":"#0a0a16",border:`2px solid ${isActive?gc:"#1a1a2e"}`,borderRadius:7,padding:"10px 8px",cursor:"pointer",textAlign:"center"}}>
                <div style={{fontSize:12,fontFamily:"monospace",fontWeight:700,color:isActive?gc:"#6a6a8a",marginBottom:2}}>{d.label}</div>
                <div style={{fontSize:8,color:isActive?"#8a8aaa":"#2a2a40",fontFamily:"monospace"}}>{d.simDays}d sim</div>
              </div>);
            })}
          </div>
          <div style={{background:"#0a0a16",border:`1px solid ${GROUP_COLORS[selectedDur.group]}44`,borderRadius:8,padding:12,marginBottom:12,display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:8}}>
            <div style={{textAlign:"center"}}><div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",letterSpacing:1,marginBottom:3}}>DURATION</div><div style={{fontSize:13,fontFamily:"monospace",fontWeight:700,color:GROUP_COLORS[selectedDur.group]}}>{selectedDur.label}</div></div>
            <div style={{textAlign:"center"}}><div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",letterSpacing:1,marginBottom:3}}>EXPIRES</div><div style={{fontSize:11,fontFamily:"monospace",fontWeight:700,color:"#c0c0e0"}}>{expiryStr}</div></div>
            <div style={{textAlign:"center"}}><div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",letterSpacing:1,marginBottom:3}}>SIGNAL ALIGN</div><div style={{fontSize:13,fontFamily:"monospace",fontWeight:700,color:tfProf.color}}>{tfScore.toFixed(0)}%</div></div>
          </div>
          <div style={{background:predColor+"10",border:`1px solid ${predColor}33`,borderRadius:8,padding:14,marginBottom:14}}>
            <div style={{fontSize:9,color:predColor,fontFamily:"monospace",letterSpacing:2,marginBottom:8}}>ENGINE PROJECTION BY {expiryStr.toUpperCase()}</div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:8,marginBottom:8}}>
              {[["TODAY",`${sym}${localPrice.toFixed(localPrice>100?0:2)}`,"#c0c0e0"],["PROJECTED",`${sym}${localSimPrice.toFixed(localSimPrice>100?0:2)}`,predColor],["MOVE",fmtPct(predictedChange),predColor]].map(([l,v,c])=>(
                <div key={l} style={{textAlign:"center"}}><div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",marginBottom:3}}>{l}</div><div style={{fontSize:14,fontFamily:"monospace",fontWeight:700,color:c}}>{v}</div></div>
              ))}
            </div>
            <div style={{height:6,background:`linear-gradient(90deg,#ff174444,#0a0a16 50%,#00e67644)`,borderRadius:3,position:"relative",marginBottom:6}}>
              <div style={{position:"absolute",top:"50%",transform:"translateY(-50%)",left:`${Math.max(5,Math.min(95,50+predictedChange*0.3))}%`,width:8,height:8,background:predColor,borderRadius:"50%",border:`2px solid ${predColor}`,marginLeft:-4}}/>
            </div>
            <div style={{textAlign:"center",fontSize:9,color:predColor,fontFamily:"monospace",fontWeight:700,marginBottom:6}}>Bias: {predictedChange>3?"BULLISH":predictedChange<-3?"BEARISH":"NEUTRAL"}</div>
            <div style={{fontSize:9,color:"#4a4a5a",lineHeight:1.6}}>Based on {selectedDur.simDays} simulated market days. Signal strength: <strong style={{color:tfProf.color}}>{tfScore.toFixed(0)}%</strong>.</div>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
            <button onClick={onClose} style={{background:"transparent",border:"1px solid #2a2a40",borderRadius:6,padding:"9px",color:"#4a4a6a",fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>CANCEL</button>
            <button onClick={()=>{
              onAdd({ticker:asset.ticker,name:asset.name,watchTimeframe:watchTF,watchDuration:selectedDur.label,priceAtAdd:signals.price,signalAtAdd:{...signals},predictedPrice:parseFloat(simPrice.toFixed(2)),predictedChangePct:parseFloat(predictedChange.toFixed(2)),simDays:selectedDur.simDays,addedAt:Date.now(),expiresAt:Date.now()+selectedDur.realMs,expiryStr,status:"watching"});
              onClose();
            }} style={{background:"#ffd60022",border:"1px solid #ffd600",borderRadius:6,padding:"9px",color:"#ffd600",fontFamily:"monospace",fontSize:9,cursor:"pointer",letterSpacing:1}}>★ START WATCHING</button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── WATCHLIST RESULT CARD ─────────────────────────────────────
function WatchResultCard({item,signals,toLocal,fmtMoney,onRemove,onTrade}){
  const now=Date.now(),elapsed=now-item.addedAt,total=item.expiresAt-item.addedAt,pct=Math.min(100,(elapsed/total)*100);
  const expired=now>=item.expiresAt;
  const tfProf=TF_PROFILES[item.watchTimeframe];
  const result=expired?scoreWatchlistPrediction(item,signals):null;
  const localEntry=fmtMoney(item.priceAtAdd);
  const localPred=fmtMoney(item.predictedPrice);

  return(
    <div style={{background:"#07070f",border:`1px solid ${expired?(result?.directionCorrect?"#00e67644":"#ff174444"):"#1a1a2e"}`,borderRadius:8,padding:14,marginBottom:8,position:"relative",overflow:"hidden"}}>
      {!expired&&<div style={{position:"absolute",top:0,left:0,height:2,background:tfProf.color,width:`${pct}%`}}/>}
      {expired&&<div style={{position:"absolute",top:0,left:0,right:0,height:2,background:result?.directionCorrect?"#00e676":"#ff1744"}}/>}
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:8}}>
        <div>
          <div style={{display:"flex",gap:6,alignItems:"center",marginBottom:2}}>
            <span style={{fontFamily:"monospace",fontSize:14,fontWeight:700,color:"#dde0ff"}}>{item.ticker}</span>
            <Tag color={tfProf.color}>{tfProf.icon} {tfProf.short}</Tag>
            {expired&&result&&<Tag color={result.gradeColor}>GRADE: {result.grade}</Tag>}
            {!expired&&<Tag color={tfProf.color}>WATCHING</Tag>}
          </div>
          <div style={{fontSize:9,color:"#3a3a55"}}>{item.name}</div>
        </div>
        <button onClick={onRemove} style={{background:"none",border:"none",color:"#333",cursor:"pointer",fontSize:12}}>×</button>
      </div>
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:6,marginBottom:10}}>
        {[["ENTRY",localEntry,"#c0c0e0"],["PREDICTED",localPred,item.predictedChangePct>0?"#00e676":"#ff1744"],["ACTUAL/SIM",expired&&result?fmtPct(result.actualMove):"pending…",expired&&result?pnlColor(result.actualMove):"#3a3a50"]].map(([l,v,c])=>(
          <div key={l} style={{background:"#0a0a16",borderRadius:5,padding:"7px",textAlign:"center"}}>
            <div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",marginBottom:2}}>{l}</div>
            <div style={{fontSize:11,fontFamily:"monospace",fontWeight:700,color:c}}>{v}</div>
          </div>
        ))}
      </div>
      {!expired&&(
        <div>
          <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}>
            <span style={{fontSize:8,color:"#3a3a50",fontFamily:"monospace"}}>Watch progress</span>
            <span style={{fontSize:8,color:tfProf.color,fontFamily:"monospace"}}>{pct.toFixed(0)}% · {fmtDuration(item.expiresAt-now)} left · {item.expiryStr}</span>
          </div>
          <div style={{height:4,background:"#0e0e18",borderRadius:2,overflow:"hidden"}}><div style={{width:`${pct}%`,height:"100%",background:tfProf.color,borderRadius:2}}/></div>
        </div>
      )}
      {expired&&result&&(
        <div style={{background:result.gradeColor+"12",border:`1px solid ${result.gradeColor}33`,borderRadius:6,padding:12}}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:8}}>
            <div><div style={{fontSize:9,color:result.gradeColor,fontFamily:"monospace",letterSpacing:2,marginBottom:2}}>PREDICTION SCORECARD</div><div style={{fontSize:11,color:"#c0c0e0",fontWeight:600}}>{result.gradeLabel}</div></div>
            <div style={{fontSize:28,fontFamily:"monospace",fontWeight:700,color:result.gradeColor}}>{result.grade}</div>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:6,marginBottom:8}}>
            {[["Predicted",fmtPct(result.expectedMove),item.predictedChangePct>0?"#00e676":"#ff1744"],["Actual",fmtPct(result.actualMove),pnlColor(result.actualMove)],["Accuracy",`${result.accuracy}%`,result.gradeColor]].map(([l,v,c])=>(
              <div key={l} style={{textAlign:"center"}}><div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",marginBottom:2}}>{l}</div><div style={{fontSize:11,fontFamily:"monospace",fontWeight:700,color:c}}>{v}</div></div>
            ))}
          </div>
          <button onClick={onTrade} style={{width:"100%",background:result.gradeColor+"22",border:`1px solid ${result.gradeColor}44`,borderRadius:5,padding:"6px",color:result.gradeColor,fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>
            {result.grade==="A"||result.grade==="B"?"📈 TRADE THIS ASSET →":"🔍 REVIEW SIGNALS FIRST →"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── ACCURACY DASHBOARD ────────────────────────────────────────
function AccuracyDashboard({trades,watchItems,currency}){
  const acc=calculateAccuracy(trades);
  const sym=currency.symbol;
  return(
    <div style={{padding:"0 2px"}}>
      <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:16,fontWeight:700,color:"#dde0ff",letterSpacing:2,marginBottom:12}}>PREDICTION ACCURACY REPORT</div>
      {acc.totalClosed===0?(
        <div style={{background:"#0a0a16",border:"1px solid #1a1a2e",borderRadius:8,padding:24,textAlign:"center"}}>
          <div style={{fontSize:28,marginBottom:8}}>📊</div>
          <div style={{fontSize:12,color:"#4a4a6a",lineHeight:1.8}}>No results yet. Place virtual trades or set watchlist timers to start building accuracy data.</div>
        </div>
      ):(
        <>
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:8,marginBottom:14}}>
            {[{label:"WIN RATE",val:`${acc.overall.toFixed(0)}%`,sub:`${acc.wins}W/${acc.losses}L`,color:ratingColor(acc.overall)},{label:"AVG RETURN",val:fmtPct(acc.avgReturn),sub:"per closed trade",color:pnlColor(acc.avgReturn)},{label:"TOTAL CLOSED",val:acc.totalClosed,sub:"virtual trades",color:"#c0c0e0"}].map(m=>(
              <div key={m.label} style={{background:"#0a0a16",border:`1px solid ${m.color}33`,borderRadius:7,padding:"10px",textAlign:"center"}}>
                <div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:3}}>{m.label}</div>
                <div style={{fontSize:20,fontFamily:"monospace",fontWeight:700,color:m.color}}>{m.val}</div>
                <div style={{fontSize:8,color:"#3a3a50"}}>{m.sub}</div>
              </div>
            ))}
          </div>
          <div style={{background:"#0a0a16",border:`1px solid ${ratingColor(acc.overall)}44`,borderRadius:8,padding:14,marginBottom:14,display:"flex",gap:10,alignItems:"center"}}>
            <div style={{fontSize:28}}>{acc.overall>=75?"🟢":acc.overall>=60?"🟡":acc.overall>=45?"🟠":"🔴"}</div>
            <div>
              <div style={{fontSize:14,fontFamily:"'Barlow Condensed',sans-serif",fontWeight:700,color:ratingColor(acc.overall)}}>{ratingLabel(acc.overall)}</div>
              <div style={{fontSize:10,color:"#5a5a7a",lineHeight:1.5}}>{acc.overall>=75?"Signal engine aligning well. Discipline is compounding.":acc.overall>=60?"Good accuracy. Keep applying 2:1 R/R discipline.":acc.overall>=45?"Mixed results — focus on process over outcome.":"Signals underperforming. Reduce size, review entry quality."}</div>
            </div>
          </div>
          {TF_KEYS.some(k=>acc.byTF[k])&&(
            <div style={{marginBottom:14}}>
              <div style={{fontSize:9,color:"#2a2a40",fontFamily:"monospace",letterSpacing:2,marginBottom:8}}>BY TIMEFRAME</div>
              {TF_KEYS.map(k=>{
                const d=acc.byTF[k];if(!d) return null;
                return(<div key={k} style={{padding:"5px 0",borderBottom:"1px solid #0a0a14"}}>
                  <div style={{display:"flex",justifyContent:"space-between",marginBottom:2}}>
                    <span style={{fontSize:9,color:"#7a7a9a"}}>{TF_PROFILES[k].icon} {TF_PROFILES[k].short}</span>
                    <span style={{fontSize:9,color:ratingColor(d.rate),fontFamily:"monospace",fontWeight:700}}>{d.rate.toFixed(0)}% ({d.wins}/{d.total})</span>
                  </div>
                  <div style={{height:3,background:"#0e0e18",borderRadius:2,overflow:"hidden"}}><div style={{width:`${d.rate}%`,height:"100%",background:ratingColor(d.rate),borderRadius:2}}/></div>
                </div>);
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── MINI SPARKLINE ────────────────────────────────────────────
function MiniSparkline({trade}){
  const asset=ASSETS.find(a=>a.ticker===trade.ticker);
  if(!asset) return null;
  const volFactor={Low:0.003,Med:0.006,High:0.012,VHigh:0.018,Extreme:0.025}[asset.volatility]||0.01;
  const tradeSeed=trade.id*7919,bullBias=(trade.signalScore/100)*0.6,tfBias=(trade.tfScore/100-0.5)*0.4;
  const steps=Math.min(30,Math.max(5,Math.round(TF_PROFILES[trade.timeframe]?.simDays||7)));
  const prices=[trade.entryPrice];let p=trade.entryPrice;
  for(let i=0;i<steps;i++){const rand=sr(tradeSeed+i*17,-1,1);p=p*(1+(bullBias+tfBias)*volFactor*0.5+rand*volFactor);prices.push(p);}
  const min=Math.min(...prices),max=Math.max(...prices),range=max-min||1,W=80,H=28;
  const pts=prices.map((v,i)=>`${(i/(prices.length-1))*W},${H-((v-min)/range)*H}`).join(" ");
  const col=prices[prices.length-1]>=trade.entryPrice?"#00e676":"#ff1744";
  return<svg width={W} height={H}><polyline points={pts} fill="none" stroke={col} strokeWidth="1.5" strokeLinecap="round"/><line x1={0} y1={H-((trade.entryPrice-min)/range)*H} x2={W} y2={H-((trade.entryPrice-min)/range)*H} stroke="#ffffff22" strokeWidth="1" strokeDasharray="3,3"/></svg>;
}

// ── MAIN APP ──────────────────────────────────────────────────
export default function MarketBrainV5(){
  const [allSignals,setAllSignals]=useState({});
  const [view,setView]=useState("market");
  const [selectedAsset,setSelectedAsset]=useState(null);
  const [watchItems,setWatchItems]=useState([]);
  const [trades,setTrades]=useState([]);
  const [nextId,setNextId]=useState(1);
  const [accountBalance,setAccountBalance]=useState(1000);
  const [startingBalance]=useState(1000);
  const [activeSector,setActiveSector]=useState("All");
  const [activeCap,setActiveCap]=useState("All");
  const [sortMode,setSortMode]=useState("bestTF");
  const [searchQ,setSearchQ]=useState("");
  const [showPlaceTrade,setShowPlaceTrade]=useState(false);
  const [showWatchModal,setShowWatchModal]=useState(false);
  const [showBudgetModal,setShowBudgetModal]=useState(false);
  const [budgetInput,setBudgetInput]=useState("1000");
  const [notifications,setNotifications]=useState([]);
  const [notifOpen,setNotifOpen]=useState(false);
  const [tick,setTick]=useState(0);
  const [expiredAlert,setExpiredAlert]=useState(null);
  const [activeCurrency,setActiveCurrency]=useState(CURRENCIES.find(c=>c.code==="GBP"));
  const [showCurrencyModal,setShowCurrencyModal]=useState(false);

  // ── currency helpers (all asset prices are stored in USD internally) ──
  // Account balance is tracked in GBP (base) internally; display converts to active currency
  const USD_TO_GBP = 1/1.27; // fixed sim rate
  const toLocalFromUsd=(usd)=>parseFloat((usd*activeCurrency.rate*USD_TO_GBP).toFixed(activeCurrency.rate>=100?0:2));
  // account balance stored in GBP; convert to local
  const balToLocal=(gbp)=>parseFloat((gbp*activeCurrency.rate).toFixed(activeCurrency.rate>=100?0:2));
  const sym=activeCurrency.symbol;
  const fmtMoney=(usd)=>{const v=toLocalFromUsd(usd);return`${sym}${v}`;};
  const fmtBal=(gbp)=>{const v=balToLocal(gbp);const d=v>1000?0:2;return`${sym}${v.toFixed(d)}`;};

  const doRefresh=useCallback(async()=>{
    const key=Date.now();const sigs={};
    ASSETS.forEach(a=>{sigs[a.ticker]=generateSignals(a,key);});
    // Attempt live price overlay from FastAPI backend
    try{
      const tickers=ASSETS.map(a=>a.ticker);
      const liveData=await fetchLivePrices(tickers);
      if(liveData){
        ASSETS.forEach(a=>{
          if(sigs[a.ticker]) sigs[a.ticker]=mergeLiveData(sigs[a.ticker],liveData,a);
        });
      }
    }catch(e){/* silent — sim prices remain */}
    setAllSignals(sigs);
  },[]);

  useEffect(()=>{doRefresh();},[]);
  useEffect(()=>{const t=setInterval(doRefresh,120000);return()=>clearInterval(t);},[]);
  useEffect(()=>{const t=setInterval(()=>setTick(s=>s+1),5000);return()=>clearInterval(t);},[]);

  useEffect(()=>{
    const now=Date.now();
    setWatchItems(prev=>prev.map(w=>{
      if(w.status==="watching"&&now>=w.expiresAt){setExpiredAlert(w);return{...w,status:"expired",simulatedFinalPrice:w.predictedPrice};}
      return w;
    }));
  },[tick]);

  useEffect(()=>{
    const watching=watchItems.filter(w=>w.status==="watching");
    if(watching.length>0&&tick%4===0){
      const w=watching[Math.floor(Math.random()*watching.length)];
      setNotifications(p=>[{msg:`⏱ WATCH — ${w.ticker}: ${fmtDuration(w.expiresAt-Date.now())} left · ${w.predictedChangePct>0?"+":""}${w.predictedChangePct.toFixed(1)}% by ${w.expiryStr}`,time:new Date().toLocaleTimeString("en-GB"),read:false},...p].slice(0,40));
    }
  },[tick,watchItems]);

  const placeTrade=(tradeData)=>{
    const id=nextId;setNextId(id+1);
    setTrades(prev=>[...prev,{id,...tradeData,status:"open",openedAt:new Date().toISOString()}]);
    setAccountBalance(prev=>parseFloat((prev-tradeData.totalCost*USD_TO_GBP).toFixed(2)));
  };

  const closeTrade=(id)=>{
    setTrades(prev=>prev.map(t=>{
      if(t.id!==id) return t;
      const sig=allSignals[t.ticker];
      const sim=sig?simulateTradeProgress(t,sig):{currentPrice:t.entryPrice,pctChange:0};
      const localSim=toLocalFromUsd(sim.currentPrice);
      const proceeds=localSim*t.shares;
      const pnl=proceeds-t.totalCost;
      const pnlPct=(pnl/t.totalCost)*100;
      setAccountBalance(prev=>parseFloat((prev+proceeds*USD_TO_GBP).toFixed(2)));
      return{...t,status:sim.status==="open"?"closed":sim.status,finalPrice:localSim,finalPnL:parseFloat(pnl.toFixed(2)),finalPnLPct:parseFloat(pnlPct.toFixed(2)),closedAt:new Date().toISOString()};
    }));
  };

  const addToWatchlist=(item)=>setWatchItems(prev=>[{...item,id:Date.now()},...prev]);
  const removeFromWatchlist=(id)=>setWatchItems(prev=>prev.filter(w=>w.id!==id));

  const pv=(()=>{
    let invested=0,currentVal=0;
    trades.filter(t=>t.status==="open").forEach(t=>{
      const sig=allSignals[t.ticker];
      const sim=sig?simulateTradeProgress(t,sig):{currentPrice:t.entryPrice/toLocalFromUsd(1)};
      invested+=t.totalCost;
      currentVal+=toLocalFromUsd(sim.currentPrice)*t.shares;
    });
    return{invested:parseFloat(invested.toFixed(2)),currentVal:parseFloat(currentVal.toFixed(2)),unrealisedPnL:parseFloat((currentVal-invested).toFixed(2))};
  })();

  const totalPnL=balToLocal(accountBalance)+pv.currentVal-balToLocal(startingBalance);
  const totalPnLPct=(totalPnL/balToLocal(startingBalance))*100;
  const unread=notifications.filter(n=>!n.read).length;
  const openTrades=trades.filter(t=>t.status==="open");
  const closedTrades=trades.filter(t=>t.status!=="open");
  const sectors=["All",...[...new Set(ASSETS.map(a=>a.sector))]];
  const acc=calculateAccuracy(trades);
  const watchingCount=watchItems.filter(w=>w.status==="watching").length;
  const USD_TO_GBP_CONST=USD_TO_GBP; // alias

  const filtered=ASSETS.filter(a=>{
    const sig=allSignals[a.ticker];if(!sig) return false;
    if(activeSector!=="All"&&a.sector!==activeSector) return false;
    if(activeCap!=="All"&&a.cap!==activeCap) return false;
    if(searchQ&&!a.ticker.toLowerCase().includes(searchQ.toLowerCase())&&!a.name.toLowerCase().includes(searchQ.toLowerCase())) return false;
    return true;
  }).sort((a,b)=>{
    const sa=allSignals[a.ticker],sb=allSignals[b.ticker];
    if(sortMode==="bestTF") return sb.tfScores[sb.bestTF]-sa.tfScores[sa.bestTF];
    if(sortMode==="score") return sb.score-sa.score;
    if(sortMode==="swing") return sb.tfScores["📈 Short Swing"]-sa.tfScores["📈 Short Swing"];
    if(sortMode==="intraday") return sb.tfScores["⚡ Intraday"]-sa.tfScores["⚡ Intraday"];
    if(sortMode==="rr") return parseFloat(sb.rrRatio)-parseFloat(sa.rrRatio);
    if(sortMode==="price_asc") return parseFloat(sa.price)-parseFloat(sb.price);
    return 0;
  });

  return(
    <div style={{height:"100vh",background:"#03030a",color:"#dde0ff",fontFamily:"'DM Mono','Courier New',monospace",display:"flex",flexDirection:"column",overflow:"hidden"}}>
      <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Barlow+Condensed:wght@300;400;600;700&display=swap" rel="stylesheet"/>

      {/* MODALS */}
      {showCurrencyModal&&<CurrencyModal current={activeCurrency} onSelect={setActiveCurrency} onClose={()=>setShowCurrencyModal(false)}/>}
      {showPlaceTrade&&selectedAsset&&allSignals[selectedAsset.ticker]&&(
        <PlaceTradeModal asset={selectedAsset} signals={allSignals[selectedAsset.ticker]} balance={balToLocal(accountBalance)} currency={activeCurrency} toLocal={toLocalFromUsd} fmtMoney={fmtMoney} onPlace={placeTrade} onClose={()=>setShowPlaceTrade(false)}/>
      )}
      {showWatchModal&&selectedAsset&&allSignals[selectedAsset.ticker]&&(
        <WatchlistModal asset={selectedAsset} signals={allSignals[selectedAsset.ticker]} currency={activeCurrency} toLocal={toLocalFromUsd} fmtMoney={fmtMoney} onAdd={addToWatchlist} onClose={()=>setShowWatchModal(false)}/>
      )}
      {expiredAlert&&(
        <div style={{position:"fixed",inset:0,background:"#000000cc",zIndex:400,display:"flex",alignItems:"center",justifyContent:"center",padding:16}}>
          <div style={{background:"#07070f",border:"1px solid #ffd600",borderRadius:12,padding:24,maxWidth:380,width:"100%",textAlign:"center",boxShadow:"0 0 60px #ffd60033"}}>
            <div style={{fontSize:32,marginBottom:8}}>⏱</div>
            <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:20,fontWeight:700,color:"#ffd600",marginBottom:8}}>WATCH PERIOD EXPIRED</div>
            <div style={{fontSize:13,color:"#c0c0e0",marginBottom:4,fontWeight:700}}>{expiredAlert.ticker} · {TF_PROFILES[expiredAlert.watchTimeframe]?.short}</div>
            <div style={{fontSize:11,color:"#6a6a8a",marginBottom:16,lineHeight:1.7}}>The watch period ended. Check the Portfolio tab to see your prediction grade.</div>
            <button onClick={()=>{setExpiredAlert(null);setView("portfolio");}} style={{width:"100%",background:"#ffd60022",border:"1px solid #ffd600",borderRadius:6,padding:"10px",color:"#ffd600",fontFamily:"monospace",fontSize:10,cursor:"pointer",letterSpacing:2}}>VIEW RESULTS →</button>
          </div>
        </div>
      )}
      {showBudgetModal&&(
        <div style={{position:"fixed",inset:0,background:"#000000cc",zIndex:300,display:"flex",alignItems:"center",justifyContent:"center",padding:20}}>
          <div style={{background:"#07070f",border:"1px solid #00b0ff",borderRadius:12,padding:24,maxWidth:320,width:"100%"}}>
            <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:17,fontWeight:700,color:"#dde0ff",marginBottom:6}}>SET VIRTUAL BUDGET</div>
            <div style={{fontSize:9,color:"#3a3a55",fontFamily:"monospace",marginBottom:12}}>Amount in {activeCurrency.code} ({sym})</div>
            <div style={{display:"flex",flexWrap:"wrap",gap:4,marginBottom:10}}>
              {["500","1000","2500","5000","10000"].map(v=>(<button key={v} onClick={()=>setBudgetInput(v)} style={{background:budgetInput===v?"#00b0ff22":"#0a0a16",border:`1px solid ${budgetInput===v?"#00b0ff":"#1a1a2e"}`,borderRadius:5,padding:"4px 10px",cursor:"pointer",color:budgetInput===v?"#00b0ff":"#5a5a7a",fontFamily:"monospace",fontSize:9}}>{sym}{v}</button>))}
            </div>
            <input value={budgetInput} onChange={e=>setBudgetInput(e.target.value)} style={{width:"100%",background:"#0e0e1c",border:"1px solid #1a1a2e",borderRadius:5,padding:"6px 8px",color:"#c0c0e0",fontFamily:"monospace",fontSize:12,outline:"none",boxSizing:"border-box",marginBottom:12}}/>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
              <button onClick={()=>setShowBudgetModal(false)} style={{background:"transparent",border:"1px solid #2a2a40",borderRadius:6,padding:"7px",color:"#4a4a6a",fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>CANCEL</button>
              <button onClick={()=>{
                const localVal=parseFloat(budgetInput)||1000;
                // convert local currency amount back to GBP for internal storage
                const gbpVal=localVal/activeCurrency.rate;
                setAccountBalance(gbpVal);setTrades([]);setWatchItems([]);setShowBudgetModal(false);
              }} style={{background:"#00b0ff22",border:"1px solid #00b0ff",borderRadius:6,padding:"7px",color:"#00b0ff",fontFamily:"monospace",fontSize:9,cursor:"pointer"}}>CONFIRM</button>
            </div>
          </div>
        </div>
      )}

      {/* HEADER */}
      <div style={{borderBottom:"1px solid #0c0c18",padding:"9px 14px",display:"flex",alignItems:"center",gap:8,flexShrink:0,background:"#03030a",zIndex:50,flexWrap:"wrap"}}>
        <div style={{marginRight:4}}>
          <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:17,fontWeight:700,letterSpacing:3,color:"#dde0ff"}}>MARKET BRAIN v5</div>
          <div style={{fontSize:7,color:"#2a2a40",letterSpacing:2}}>VIRTUAL ACCOUNT · SMART WATCHLIST</div>
        </div>
        <div style={{display:"flex",gap:5,flexWrap:"wrap"}}>
          {[{label:"CASH",val:fmtBal(accountBalance),color:"#00b0ff"},{label:"INVESTED",val:`${sym}${pv.invested.toFixed(pv.invested>100?0:2)}`,color:"#ffd600"},{label:"UNREALISED",val:fmtPct(pv.invested>0?pv.unrealisedPnL/pv.invested*100:0),color:pnlColor(pv.unrealisedPnL)},{label:"TOTAL P&L",val:`${totalPnL>=0?"+":""}${sym}${Math.abs(totalPnL).toFixed(Math.abs(totalPnL)>100?0:2)}`,color:pnlColor(totalPnL)}].map(m=>(
            <div key={m.label} style={{background:"#0a0a16",border:"1px solid #12121e",borderRadius:4,padding:"3px 7px",textAlign:"center"}}>
              <div style={{fontSize:6,color:"#2a2a40",fontFamily:"monospace",letterSpacing:1}}>{m.label}</div>
              <div style={{fontSize:10,color:m.color,fontFamily:"monospace",fontWeight:700}}>{m.val}</div>
            </div>
          ))}
          {acc.totalClosed>0&&<div onClick={()=>setView("accuracy")} style={{background:"#0a0a16",border:`1px solid ${ratingColor(acc.overall)}33`,borderRadius:4,padding:"3px 7px",cursor:"pointer",textAlign:"center"}}><div style={{fontSize:6,color:"#2a2a40",fontFamily:"monospace"}}>ACCURACY</div><div style={{fontSize:10,color:ratingColor(acc.overall),fontFamily:"monospace",fontWeight:700}}>{acc.overall.toFixed(0)}%</div></div>}
        </div>
        <div style={{marginLeft:"auto",display:"flex",gap:6,alignItems:"center"}}>
          {/* CURRENCY SELECTOR */}
          <button onClick={()=>setShowCurrencyModal(true)} style={{background:"#0a0a16",border:"1px solid #00b0ff44",borderRadius:5,padding:"4px 9px",cursor:"pointer",display:"flex",alignItems:"center",gap:5}}>
            <span style={{fontSize:14}}>{activeCurrency.flag}</span>
            <span style={{fontSize:9,color:"#00b0ff",fontFamily:"monospace",fontWeight:700}}>{activeCurrency.code}</span>
            <span style={{fontSize:8,color:"#2a2a40"}}>▾</span>
          </button>
          <button onClick={()=>setShowBudgetModal(true)} style={{background:"#0a0a16",border:"1px solid #1a1a2e",borderRadius:5,padding:"4px 8px",cursor:"pointer",color:"#00b0ff",fontSize:8,fontFamily:"monospace"}}>💰 BUDGET</button>
          <button onClick={doRefresh} style={{background:"#0a0a16",border:"1px solid #1a1a2e",borderRadius:5,padding:"4px 7px",cursor:"pointer",color:"#5a5a8a",fontSize:10,fontFamily:"monospace"}}>↻</button>
          <div style={{position:"relative"}}>
            <button onClick={()=>setNotifOpen(!notifOpen)} style={{background:"#0a0a16",border:"1px solid #1a1a2e",borderRadius:5,padding:"4px 8px",cursor:"pointer",color:"#5a5a8a",fontSize:11,display:"flex",alignItems:"center",gap:3}}>
              🔔{unread>0&&<span style={{background:"#ff1744",color:"#fff",borderRadius:10,padding:"0 3px",fontSize:7,fontWeight:700}}>{unread}</span>}
            </button>
            {notifOpen&&(<div style={{position:"absolute",right:0,top:34,width:300,background:"#06060f",border:"1px solid #1a1a2e",borderRadius:8,zIndex:100,maxHeight:280,overflow:"auto",boxShadow:"0 20px 60px #000c"}}>
              <div style={{padding:"6px 10px",borderBottom:"1px solid #0e0e18",display:"flex",justifyContent:"space-between"}}><span style={{fontSize:7,color:"#2a2a40",letterSpacing:2}}>UPDATES</span><button onClick={()=>setNotifications(p=>p.map(n=>({...n,read:true})))} style={{background:"none",border:"none",color:"#2a2a40",cursor:"pointer",fontSize:7}}>CLEAR</button></div>
              {notifications.length===0&&<div style={{padding:14,textAlign:"center",color:"#2a2a40",fontSize:9}}>No updates yet</div>}
              {notifications.map((n,i)=>(<div key={i} style={{padding:"7px 10px",borderBottom:"1px solid #0a0a12",background:n.read?"transparent":"#0a0a16"}}><div style={{fontSize:9,color:"#8a8aaa",lineHeight:1.5}}>{n.msg}</div><div style={{fontSize:7,color:"#2a2a40",marginTop:1}}>{n.time}</div></div>))}
            </div>)}
          </div>
        </div>
      </div>

      {/* NAV */}
      <div style={{borderBottom:"1px solid #0c0c18",padding:"0 14px",display:"flex",gap:0,flexShrink:0,background:"#04040c"}}>
        {[["market","📊 Market"],["portfolio","💼 Portfolio / Watchlist"],["accuracy","🎯 Accuracy"]].map(([v,l])=>(
          <button key={v} onClick={()=>setView(v)} style={{background:"transparent",border:"none",borderBottom:`2px solid ${view===v?"#00b0ff":"transparent"}`,padding:"9px 14px",cursor:"pointer",color:view===v?"#00b0ff":"#3a3a55",fontFamily:"monospace",fontSize:9,letterSpacing:1,whiteSpace:"nowrap"}}>
            {l}{v==="portfolio"&&(openTrades.length+watchingCount)>0&&<span style={{marginLeft:5,background:"#ffd60033",color:"#ffd600",borderRadius:10,padding:"0 5px",fontSize:7}}>{openTrades.length+watchingCount}</span>}
          </button>
        ))}
      </div>

      {/* MARKET VIEW */}
      {view==="market"&&(
        <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
          <div style={{borderBottom:"1px solid #0c0c18",padding:"6px 12px",background:"#04040c",flexShrink:0}}>
            <div style={{display:"flex",gap:4,flexWrap:"wrap",alignItems:"center",marginBottom:4}}>
              <input value={searchQ} onChange={e=>setSearchQ(e.target.value)} placeholder="search..." style={{background:"#0a0a16",border:"1px solid #1a1a2e",borderRadius:4,padding:"3px 7px",color:"#8a8aaa",fontSize:9,fontFamily:"monospace",width:90,outline:"none"}}/>
              {["All","Nano","Micro","Small","Mid","Large"].map(c=>(<Pill key={c} label={c} active={activeCap===c} color={CAP_COLORS[c]} onClick={()=>setActiveCap(c)}/>))}
            </div>
            <div style={{display:"flex",gap:4,flexWrap:"wrap",alignItems:"center"}}>
              {sectors.map(s=>(<Pill key={s} label={s==="All"?"ALL":s.slice(0,6).toUpperCase()} active={activeSector===s} color={"#00b0ff"} onClick={()=>setActiveSector(s)}/>))}
              <div style={{marginLeft:"auto",display:"flex",gap:3}}>
                {[["bestTF","⭐Best"],["score","Brain"],["swing","📈Swing"],["intraday","⚡Day"],["rr","R/R"],["price_asc","Cheap"]].map(([v,l])=>(<Pill key={v} label={l} active={sortMode===v} onClick={()=>setSortMode(v)}/>))}
              </div>
            </div>
          </div>
          <div style={{flex:1,display:"grid",gridTemplateColumns:selectedAsset?"1fr 370px":"1fr",overflow:"hidden",minHeight:0}}>
            <div style={{overflowY:"auto",padding:10}}>
              <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(175px,1fr))",gap:6}}>
                {filtered.map(a=>{
                  const sig=allSignals[a.ticker];if(!sig) return null;
                  const R=RISK_CFG[sig.risk];
                  const bestProf=TF_PROFILES[sig.bestTF];
                  const bScore=sig.tfScores[sig.bestTF];
                  const pc=bScore>=72?"#00e676":bScore>=52?"#00d4ff":bScore>=35?"#ffd600":"#ff6d00";
                  const isSelected=selectedAsset?.ticker===a.ticker;
                  return(
                    <div key={a.ticker} onClick={()=>setSelectedAsset(isSelected?null:a)} style={{background:isSelected?R.bg:"#07070f",border:`1px solid ${isSelected?R.color:"#12121e"}`,borderRadius:7,padding:"10px 11px",cursor:"pointer",position:"relative",overflow:"hidden",transition:"border-color 0.15s"}}
                      onMouseEnter={e=>{if(!isSelected)e.currentTarget.style.borderColor=R.color+"55";}} onMouseLeave={e=>{if(!isSelected)e.currentTarget.style.borderColor="#12121e";}}>
                      <div style={{position:"absolute",top:0,left:0,right:0,height:2,background:R.color}}/>
                      <div style={{display:"flex",justifyContent:"space-between",marginBottom:2}}>
                        <span style={{fontFamily:"monospace",fontSize:12,fontWeight:700,color:"#dde0ff"}}>{a.ticker}</span>
                        <span style={{fontSize:7,color:R.color,fontFamily:"monospace",border:`1px solid ${R.color}44`,borderRadius:3,padding:"1px 4px"}}>{R.label}</span>
                      </div>
                      <div style={{fontSize:8,color:"#2a2a3a",marginBottom:5}}>{a.name}</div>
                      <div style={{background:bestProf.color+"18",border:`1px solid ${bestProf.color}33`,borderRadius:4,padding:"3px 6px",marginBottom:5,display:"flex",justifyContent:"space-between"}}>
                        <span style={{fontSize:8,color:bestProf.color,fontFamily:"monospace"}}>{bestProf.icon} {bestProf.short}</span>
                        <span style={{fontSize:9,color:pc,fontFamily:"monospace",fontWeight:700}}>{bScore.toFixed(0)}%</span>
                      </div>
                      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                        <span style={{fontFamily:"monospace",fontSize:10,color:"#b0b0cc"}}>{fmtMoney(sig.price)}</span>
                        <span style={{fontSize:8,color:CAP_COLORS[a.cap],fontFamily:"monospace"}}>{a.cap}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
            {selectedAsset&&allSignals[selectedAsset.ticker]&&(()=>{
              const sig=allSignals[selectedAsset.ticker];const R=RISK_CFG[sig.risk];
              return(
                <div style={{borderLeft:"1px solid #0c0c18",overflowY:"auto",padding:12,background:"#04040c"}}>
                  <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:10}}>
                    <div>
                      <div style={{fontFamily:"'Barlow Condensed',sans-serif",fontSize:18,fontWeight:700,color:"#dde0ff"}}>{selectedAsset.ticker}</div>
                      <div style={{fontSize:9,color:"#3a3a55"}}>{selectedAsset.name} · {selectedAsset.sub}</div>
                    </div>
                    <div style={{textAlign:"right"}}>
                      <div style={{fontFamily:"monospace",fontSize:15,fontWeight:700,color:"#dde0ff"}}>{fmtMoney(sig.price)}</div>
                      <div style={{fontSize:8,color:"#3a3a40",fontFamily:"monospace",display:"flex",gap:4,alignItems:"center",justifyContent:"flex-end"}}>
                        {sig.livePrice
                          ? <span style={{color:"#00e676",fontSize:7,border:"1px solid #00e67644",borderRadius:3,padding:"0 4px"}}>● LIVE · {sig.marketState||""}</span>
                          : <span style={{color:"#3a3a55",fontSize:7,border:"1px solid #2a2a3a",borderRadius:3,padding:"0 4px"}}>SIM</span>}
                      </div>
                      <Tag color={R.color}>{R.label}</Tag>
                    </div>
                  </div>
                  <div style={{background:"#0a0a16",borderRadius:6,padding:10,marginBottom:10}}>
                    {TF_KEYS.map(k=>{
                      const p=TF_PROFILES[k],sc=sig.tfScores[k];
                      const pc=sc>=72?"#00e676":sc>=52?"#00d4ff":sc>=35?"#ffd600":"#ff6d00";
                      const isBest=sig.bestTF===k;
                      return(<div key={k} style={{marginBottom:6}}>
                        <div style={{display:"flex",justifyContent:"space-between",marginBottom:2}}>
                          <div style={{display:"flex",gap:4,alignItems:"center"}}><span style={{fontSize:9}}>{p.icon}</span><span style={{fontSize:8,color:isBest?p.color:"#5a5a7a",fontFamily:"monospace",fontWeight:isBest?"700":"normal"}}>{p.short}</span>{isBest&&<span style={{fontSize:6,color:p.color,fontFamily:"monospace",border:`1px solid ${p.color}`,borderRadius:8,padding:"0 3px"}}>BEST</span>}</div>
                          <span style={{fontSize:9,color:pc,fontFamily:"monospace",fontWeight:700}}>{sc.toFixed(0)}%</span>
                        </div>
                        <div style={{height:3,background:"#0e0e18",borderRadius:2,overflow:"hidden"}}><div style={{width:`${sc}%`,height:"100%",background:`linear-gradient(90deg,${pc}66,${pc})`,borderRadius:2}}/></div>
                      </div>);
                    })}
                  </div>
                  <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:5,marginBottom:10}}>
                    {[{l:"R/R",v:`${sig.rrRatio}:1`,c:parseFloat(sig.rrRatio)>=2?"#00e676":"#ffd600"},{l:"UPSIDE",v:`+${sig.upsidePct}%`,c:"#00e676"},{l:"STOP",v:`-${sig.stopPct}%`,c:"#ff1744"},{l:"ENTRY Q",v:sig.entryQ,c:sig.entryQ==="IDEAL"?"#00e676":sig.entryQ==="GOOD"?"#00b0ff":sig.entryQ==="FAIR"?"#ffd600":"#ff6d00"},{l:"RSI",v:sig.metrics.rsi.toFixed(0),c:sig.metrics.rsi<30?"#00e676":sig.metrics.rsi>70?"#ff1744":"#c0c0e0"},{l:"VOL ×",v:sig.metrics.volume.toFixed(1)+"×",c:sig.metrics.volume>1.5?"#00d4ff":"#c0c0e0"}].map(m=>(
                      <div key={m.l} style={{background:"#0a0a16",borderRadius:4,padding:"6px",textAlign:"center"}}>
                        <div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace",marginBottom:1}}>{m.l}</div>
                        <div style={{fontSize:10,fontFamily:"monospace",fontWeight:700,color:m.c}}>{m.v}</div>
                      </div>
                    ))}
                  </div>
                  <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:7,marginBottom:8}}>
                    <button onClick={()=>setShowWatchModal(true)} style={{background:"#ffd60022",border:"1px solid #ffd600",borderRadius:6,padding:"9px",color:"#ffd600",fontFamily:"monospace",fontSize:9,cursor:"pointer",letterSpacing:1}}>★ WATCH & TIME</button>
                    <button onClick={()=>setShowPlaceTrade(true)} disabled={balToLocal(accountBalance)<toLocalFromUsd(sig.price)} style={{background:balToLocal(accountBalance)>=toLocalFromUsd(sig.price)?R.color+"22":"#1a1a2e",border:`1px solid ${balToLocal(accountBalance)>=toLocalFromUsd(sig.price)?R.color:"#2a2a40"}`,borderRadius:6,padding:"9px",color:balToLocal(accountBalance)>=toLocalFromUsd(sig.price)?R.color:"#3a3a50",fontFamily:"monospace",fontSize:9,cursor:balToLocal(accountBalance)>=toLocalFromUsd(sig.price)?"pointer":"not-allowed",letterSpacing:1}}>📈 TRADE</button>
                  </div>
                  <div style={{fontSize:7,color:"#1a1a28",fontFamily:"monospace",textAlign:"center"}}>Virtual simulation only. Not financial advice.</div>
                </div>
              );
            })()}
          </div>
        </div>
      )}

      {/* PORTFOLIO / WATCHLIST VIEW */}
      {view==="portfolio"&&(
        <div style={{flex:1,overflowY:"auto",padding:12}}>
          {watchItems.length>0&&(
            <div style={{marginBottom:18}}>
              <div style={{fontSize:9,color:"#ffd600",fontFamily:"monospace",letterSpacing:2,marginBottom:10}}>★ TIMED WATCHLIST ({watchItems.length}) — {watchingCount} ACTIVE</div>
              {[...watchItems].sort((a,b)=>a.expiresAt-b.expiresAt).map(item=>(
                <WatchResultCard key={item.id} item={item} signals={allSignals[item.ticker]} toLocal={toLocalFromUsd} fmtMoney={fmtMoney}
                  onRemove={()=>removeFromWatchlist(item.id)}
                  onTrade={()=>{const a=ASSETS.find(x=>x.ticker===item.ticker);if(a){setSelectedAsset(a);setView("market");setTimeout(()=>setShowPlaceTrade(true),200);}}}
                />
              ))}
            </div>
          )}
          <div style={{marginBottom:18}}>
            <div style={{fontSize:9,color:"#00e676",fontFamily:"monospace",letterSpacing:2,marginBottom:10}}>📈 OPEN POSITIONS ({openTrades.length})</div>
            {openTrades.length===0?(
              <div style={{background:"#07070f",border:"1px solid #1a1a2e",borderRadius:7,padding:16,textAlign:"center",color:"#3a3a55",fontSize:10}}>No open positions. Browse the Market tab to place a virtual trade.</div>
            ):openTrades.map(t=>{
              const sig=allSignals[t.ticker];
              const sim=sig?simulateTradeProgress(t,sig):{currentPrice:t.entryPrice,pctChange:0};
              const localSim=toLocalFromUsd(sim.currentPrice);
              const pnl=(localSim-t.entryPrice)*t.shares;
              const pnlPct=((localSim-t.entryPrice)/t.entryPrice)*100;
              const R=RISK_CFG[t.signalRisk];const tfP=TF_PROFILES[t.timeframe];
              return(
                <div key={t.id} style={{background:"#07070f",border:"1px solid #1a1a2e",borderRadius:8,padding:12,marginBottom:7,borderLeft:`3px solid ${tfP.color}`}}>
                  <div style={{display:"grid",gridTemplateColumns:"1fr auto",gap:8,alignItems:"start"}}>
                    <div>
                      <div style={{display:"flex",gap:6,alignItems:"center",marginBottom:5,flexWrap:"wrap"}}>
                        <span style={{fontFamily:"monospace",fontSize:13,fontWeight:700,color:"#dde0ff"}}>{t.ticker}</span>
                        <Tag color={tfP.color}>{tfP.icon} {tfP.short}</Tag>
                        <Tag color={R.color}>{R.label}</Tag>
                        <span style={{fontSize:8,color:"#3a3a55"}}>{t.shares} shares</span>
                      </div>
                      <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:5,marginBottom:7}}>
                        {[{l:"ENTRY",v:`${sym}${t.entryPrice.toFixed(t.entryPrice>100?0:2)}`,c:"#c0c0e0"},{l:"NOW",v:`${sym}${localSim.toFixed(localSim>100?0:2)}`,c:pnlColor(pnlPct)},{l:"P&L%",v:fmtPct(pnlPct),c:pnlColor(pnlPct)},{l:"STOP",v:`-${t.stopLossPct}%`,c:"#ff174477"},{l:"TARGET",v:`+${t.targetPct}%`,c:"#00e67677"}].map(m=>(
                          <div key={m.l} style={{background:"#0a0a16",borderRadius:4,padding:"5px",textAlign:"center"}}>
                            <div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace"}}>{m.l}</div>
                            <div style={{fontSize:9,fontFamily:"monospace",fontWeight:700,color:m.c}}>{m.v}</div>
                          </div>
                        ))}
                      </div>
                      <div style={{display:"flex",gap:10,alignItems:"center"}}>
                        <MiniSparkline trade={t}/>
                        <div style={{textAlign:"center"}}><div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace"}}>SIGNAL PROB</div><div style={{fontSize:13,fontFamily:"monospace",fontWeight:700,color:tfP.color}}>{t.tfScore}%</div></div>
                        <div style={{textAlign:"center"}}><div style={{fontSize:7,color:"#2a2a40",fontFamily:"monospace"}}>UNREALISED</div><div style={{fontSize:12,fontFamily:"monospace",fontWeight:700,color:pnlColor(pnl)}}>{pnl>=0?"+":"-"}{sym}{Math.abs(pnl).toFixed(Math.abs(pnl)>100?0:2)}</div></div>
                      </div>
                    </div>
                    <button onClick={()=>closeTrade(t.id)} style={{background:"#ff174422",border:"1px solid #ff174466",borderRadius:5,padding:"7px 9px",color:"#ff1744",fontFamily:"monospace",fontSize:8,cursor:"pointer",whiteSpace:"nowrap"}}>CLOSE</button>
                  </div>
                </div>
              );
            })}
          </div>
          {closedTrades.length>0&&(
            <div>
              <div style={{fontSize:9,color:"#5a5a7a",fontFamily:"monospace",letterSpacing:2,marginBottom:8}}>📁 CLOSED TRADES ({closedTrades.length})</div>
              {closedTrades.map(t=>{
                const tfP=TF_PROFILES[t.timeframe];const won=t.finalPnLPct>0;
                return(
                  <div key={t.id} style={{background:"#06060e",border:`1px solid ${won?"#00e67622":"#ff174422"}`,borderRadius:6,padding:"9px 12px",marginBottom:5,display:"grid",gridTemplateColumns:"1fr auto",gap:8,alignItems:"center"}}>
                    <div>
                      <div style={{display:"flex",gap:5,alignItems:"center",marginBottom:3,flexWrap:"wrap"}}>
                        <span style={{fontFamily:"monospace",fontSize:11,fontWeight:700,color:won?"#00e676":"#ff1744"}}>{t.ticker}</span>
                        <Tag color={tfP.color}>{tfP.icon} {tfP.short}</Tag>
                        {t.status==="target_hit"&&<Tag color="#00e676">TARGET ✓</Tag>}
                        {t.status==="stopped"&&<Tag color="#ff1744">STOPPED</Tag>}
                      </div>
                      <div style={{fontSize:8,color:"#4a4a6a"}}>Entry: <strong style={{color:"#c0c0e0"}}>{sym}{t.entryPrice.toFixed(t.entryPrice>100?0:2)}</strong> → Exit: <strong style={{color:"#c0c0e0"}}>{sym}{t.finalPrice?.toFixed(t.finalPrice>100?0:2)}</strong></div>
                    </div>
                    <div style={{textAlign:"right"}}>
                      <div style={{fontFamily:"monospace",fontSize:13,fontWeight:700,color:pnlColor(t.finalPnL)}}>{t.finalPnL>=0?"+":"-"}{sym}{Math.abs(t.finalPnL).toFixed(Math.abs(t.finalPnL)>100?0:2)}</div>
                      <div style={{fontSize:9,color:pnlColor(t.finalPnLPct),fontFamily:"monospace"}}>{fmtPct(t.finalPnLPct)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {view==="accuracy"&&(
        <div style={{flex:1,overflowY:"auto",padding:12}}>
          <AccuracyDashboard trades={trades} watchItems={watchItems} currency={activeCurrency}/>
        </div>
      )}

      {notifications.length>0&&(
        <div style={{borderTop:"1px solid #0e0e18",padding:"4px 12px",display:"flex",alignItems:"center",gap:8,flexShrink:0,background:"#06060f"}}>
          <span style={{fontSize:7,color:"#ff1744",fontFamily:"monospace",animation:"pulse 2s infinite",whiteSpace:"nowrap"}}>● LIVE</span>
          <span style={{fontSize:9,color:"#6a6a8a",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",flex:1}}>{notifications[0]?.msg}</span>
        </div>
      )}
      <style>{`@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}`}</style>
    </div>
  );
}
