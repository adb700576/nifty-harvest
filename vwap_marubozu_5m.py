import math
import pandas as pd
import numpy as np
from datetime import datetime


def round_to_tick(price: float, tick_size: float = 0.05) -> float:
    ts = tick_size if tick_size and tick_size > 0 else 0.05
    decimals = max(0, len(f"{ts:.10f}".rstrip("0").split(".")[1])) if "." in f"{ts:.10f}".rstrip("0") else 0
    return round(round(price / ts) * ts, decimals)


def prepare_5m_indicators(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Resamples 1m intraday candles to completed 5m candles with VWAP, 20 EMA, ATR14, and Body/Vol ratios."""
    if df_1m.empty or len(df_1m) < 5:
        return pd.DataFrame()

    tmp = df_1m.copy().set_index("timestamp")
    df_5m = tmp.resample("5min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()

    if df_5m.empty:
        return df_5m

    df_5m["ema20"] = df_5m["close"].ewm(span=20, adjust=False).mean()

    prev_close = df_5m["close"].shift(1).fillna(df_5m["open"])
    tr = pd.concat([
        df_5m["high"] - df_5m["low"],
        (df_5m["high"] - prev_close).abs(),
        (df_5m["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df_5m["atr14"] = tr.rolling(14, min_periods=1).mean()

    tp = (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3.0
    day_ser = df_5m["timestamp"].dt.date
    vol_safe = df_5m["volume"].replace(0, 1.0)
    df_5m["vwap"] = (tp * vol_safe).groupby(day_ser).cumsum() / vol_safe.groupby(day_ser).cumsum()

    vol_sma20 = df_5m["volume"].rolling(20, min_periods=1).mean().replace(0, np.nan)
    df_5m["vol_ratio"] = (df_5m["volume"] / vol_sma20).fillna(1.0)

    c_range = (df_5m["high"] - df_5m["low"]).replace(0, 1e-6)
    df_5m["body_ratio"] = (df_5m["close"] - df_5m["open"]).abs() / c_range
    df_5m["upper_wick_ratio"] = (df_5m["high"] - df_5m[["open", "close"]].max(axis=1)) / c_range
    df_5m["lower_wick_ratio"] = (df_5m[["open", "close"]].min(axis=1) - df_5m["low"]) / c_range
    return df_5m


class FiveMinVwapMarubozuStrategy:
    """
    5-Minute VWAP Marubozu Arm + Pullback Rejection Entry + 1:1 / 1:2 / Infinity Trail + PCR/Momentum Melt Exit.
    Handles:
      - PE Flow: Bearish Marubozu cuts VWAP -> Pullback -> Any Bearish Rejection Candle close -> PE Entry
      - CE Flow: Bullish Marubozu cuts VWAP -> Pullback -> Any Bullish Rejection Candle close -> CE Entry
    """

    def __init__(
        self,
        marubozu_body_min: float = 0.62,
        marubozu_vol_min: float = 1.15,
        sl_atr_buffer: float = 0.15,
        max_arm_bars: int = 24,          # Up to 2 hours (24 x 5m bars, e.g., 10:50 AM -> 12:00 PM)
        stall_bars_limit: int = 3,       # Exit if 3 closed 5m bars stall with weak volume
        pcr_melt_threshold: float = 0.08 # Exit if PCR shifts against position by >= 0.08
    ):
        self.marubozu_body_min = marubozu_body_min
        self.marubozu_vol_min = marubozu_vol_min
        self.sl_atr_buffer = sl_atr_buffer
        self.max_arm_bars = max_arm_bars
        self.stall_bars_limit = stall_bars_limit
        self.pcr_melt_threshold = pcr_melt_threshold

        self.armed_setup = {}       # idx -> dict with arm state
        self.last_eval_bar_ts = {}  # idx -> last closed 5m bar timestamp

    def _is_bearish_marubozu_vwap_cut(self, bar: pd.Series) -> bool:
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vwap = float(bar["vwap"])
        body_r = float(bar["body_ratio"])
        vol_r = float(bar["vol_ratio"])
        # Strong red candle cutting decisively below VWAP
        cut_vwap = (o >= vwap or h >= vwap) and (c < vwap)
        return (c < o) and cut_vwap and (body_r >= self.marubozu_body_min) and (vol_r >= self.marubozu_vol_min)

    def _is_bullish_marubozu_vwap_cut(self, bar: pd.Series) -> bool:
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        vwap = float(bar["vwap"])
        body_r = float(bar["body_ratio"])
        vol_r = float(bar["vol_ratio"])
        # Strong green candle cutting decisively above VWAP
        cut_vwap = (o <= vwap or l <= vwap) and (c > vwap)
        return (c > o) and cut_vwap and (body_r >= self.marubozu_body_min) and (vol_r >= self.marubozu_vol_min)

    def _is_bearish_rejection_candle(self, curr: pd.Series, prev: pd.Series) -> tuple:
        """Detects any bearish rejection candle closing below VWAP & 20 EMA after pullback."""
        o, h, l, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        vwap, ema20, atr = float(curr["vwap"]), float(curr["ema20"]), max(float(curr["atr14"]), 1e-6)
        u_wick = float(curr["upper_wick_ratio"])
        body_r = float(curr["body_ratio"])

        # Must reject downward and close below both VWAP and 20 EMA
        if not (c < vwap and c < ema20):
            return False, ""

        # Pulled back toward VWAP or 20 EMA (within 0.6 ATR)
        near_resistance = (h >= min(vwap, ema20) - 0.60 * atr) or (float(prev["high"]) >= min(vwap, ema20) - 0.60 * atr)
        if not near_resistance:
            return False, ""

        if u_wick >= 0.40 and c <= o:
            return True, "WICK_REJECT_PE"
        if (float(prev["close"]) > float(prev["open"])) and (c < o) and (c <= float(prev["open"])):
            return True, "BEAR_ENGULF_PE"
        if (c < o) and (body_r >= 0.45) and (c < float(prev["low"])):
            return True, "EMA_VWAP_REJECT_PE"
        if (c < o) and (u_wick >= 0.30):
            return True, "PULLBACK_REJECT_PE"

        return False, ""

    def _is_bullish_rejection_candle(self, curr: pd.Series, prev: pd.Series) -> tuple:
        """Exact reverse: detects any bullish rejection candle closing above VWAP & 20 EMA after pullback."""
        o, h, l, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        vwap, ema20, atr = float(curr["vwap"]), float(curr["ema20"]), max(float(curr["atr14"]), 1e-6)
        l_wick = float(curr["lower_wick_ratio"])
        body_r = float(curr["body_ratio"])

        # Must reject upward and close above both VWAP and 20 EMA
        if not (c > vwap and c > ema20):
            return False, ""

        # Pulled back toward VWAP or 20 EMA (within 0.6 ATR)
        near_support = (l <= max(vwap, ema20) + 0.60 * atr) or (float(prev["low"]) <= max(vwap, ema20) + 0.60 * atr)
        if not near_support:
            return False, ""

        if l_wick >= 0.40 and c >= o:
            return True, "WICK_REJECT_CE"
        if (float(prev["close"]) < float(prev["open"])) and (c > o) and (c >= float(prev["open"])):
            return True, "BULL_ENGULF_CE"
        if (c > o) and (body_r >= 0.45) and (c > float(prev["high"])):
            return True, "EMA_VWAP_REJECT_CE"
        if (c > o) and (l_wick >= 0.30):
            return True, "PULLBACK_REJECT_CE"

        return False, ""

    def evaluate_5m_signal(
        self,
        idx: str,
        df_5m: pd.DataFrame,
        pcr: float,
        pcr_prev: float,
        has_open_position: bool,
        tick_size: float = 0.05,
    ) -> dict:
        """
        Call this on every cycle with 5m resampled DataFrame.
        Evaluates strictly on completed 5m bars (`df_5m.iloc[-2]`).
        """
        if df_5m.empty or len(df_5m) < 4:
            return {"action": "NONE", "status": "WARMUP_5M"}

        closed_bar = df_5m.iloc[-2]
        prev_bar = df_5m.iloc[-3]
        bar_ts = str(closed_bar["timestamp"])
        bar_hhmm = closed_bar["timestamp"].strftime("%H:%M")

        # Only evaluate once per completed 5m candle close (e.g., 09:15, 09:40, 10:50, 12:00, 13:10, 13:20)
        if self.last_eval_bar_ts.get(idx) == bar_ts:
            arm = self.armed_setup.get(idx)
            st = f"ARMED_{arm['side']}@{arm['arm_time']}(SL:{arm['spot_sl']:.1f})" if arm else "SCAN_5M_VWAP"
            return {"action": "NONE", "status": st}

        self.last_eval_bar_ts[idx] = bar_ts
        close_px = float(closed_bar["close"])
        high_px = float(closed_bar["high"])
        low_px = float(closed_bar["low"])
        atr = max(float(closed_bar["atr14"]), tick_size * 4)

        arm = self.armed_setup.get(idx)

        # 1. Check invalidation or expiry of existing ARM
        if arm is not None:
            arm["bars_elapsed"] += 1
            if arm["bars_elapsed"] > self.max_arm_bars:
                self.armed_setup[idx] = None
                arm = None
            elif arm["side"] == "PE" and high_px >= arm["spot_sl"]:
                self.armed_setup[idx] = None
                arm = None
            elif arm["side"] == "CE" and low_px <= arm["spot_sl"]:
                self.armed_setup[idx] = None
                arm = None

        # 2. Check if a NEW strong Marubozu cuts VWAP -> ARM (or flip ARM)
        if self._is_bearish_marubozu_vwap_cut(closed_bar):
            buf = self.sl_atr_buffer * atr
            spot_sl = round_to_tick(high_px + buf, tick_size)
            self.armed_setup[idx] = {
                "side": "PE",
                "arm_time": bar_hhmm,
                "arm_bar_ts": bar_ts,
                "spot_sl": spot_sl,
                "arm_close": close_px,
                "bars_elapsed": 0,
                "pulled_back": False,
            }
            return {
                "action": "ARMED",
                "side": "PE",
                "arm_time": bar_hhmm,
                "spot_sl": spot_sl,
                "status": f"PE_ARMED@{bar_hhmm}(SL:{spot_sl:.1f})",
            }

        if self._is_bullish_marubozu_vwap_cut(closed_bar):
            buf = self.sl_atr_buffer * atr
            spot_sl = round_to_tick(low_px - buf, tick_size)
            self.armed_setup[idx] = {
                "side": "CE",
                "arm_time": bar_hhmm,
                "arm_bar_ts": bar_ts,
                "spot_sl": spot_sl,
                "arm_close": close_px,
                "bars_elapsed": 0,
                "pulled_back": False,
            }
            return {
                "action": "ARMED",
                "side": "CE",
                "arm_time": bar_hhmm,
                "spot_sl": spot_sl,
                "status": f"CE_ARMED@{bar_hhmm}(SL:{spot_sl:.1f})",
            }

        # 3. If ARMED and no open position yet, check for Pullback + Rejection Entry on 5m candle close
        if arm is not None and not has_open_position and bar_ts != arm["arm_bar_ts"]:
            if arm["side"] == "PE":
                # Track pullback toward VWAP / 20 EMA
                if high_px > float(prev_bar["high"]) or close_px > float(prev_bar["close"]):
                    arm["pulled_back"] = True

                rej_ok, pat_name = self._is_bearish_rejection_candle(closed_bar, prev_bar)
                pcr_ok = (pcr <= 1.05) and (pcr <= pcr_prev + 0.01)

                if rej_ok and pcr_ok:
                    spot_risk = arm["spot_sl"] - close_px
                    if spot_risk > 0:
                        self.armed_setup[idx] = None
                        return {
                            "action": "ENTER",
                            "side": "PE",
                            "setup": f"5M_VWAP_{pat_name}",
                            "arm_time": arm["arm_time"],
                            "entry_time": bar_hhmm,
                            "spot_entry": close_px,
                            "spot_sl": arm["spot_sl"],
                            "spot_risk": round(spot_risk, 2),
                            "spot_t1": round_to_tick(close_px - spot_risk, tick_size),         # 1:1
                            "spot_t2": round_to_tick(close_px - (2.0 * spot_risk), tick_size), # 1:2
                            "entry_pcr": pcr,
                            "status": f"ENTER_PE@{bar_hhmm}",
                        }

            elif arm["side"] == "CE":
                if low_px < float(prev_bar["low"]) or close_px < float(prev_bar["close"]):
                    arm["pulled_back"] = True

                rej_ok, pat_name = self._is_bullish_rejection_candle(closed_bar, prev_bar)
                pcr_ok = (pcr >= 0.95) and (pcr >= pcr_prev - 0.01)

                if rej_ok and pcr_ok:
                    spot_risk = close_px - arm["spot_sl"]
                    if spot_risk > 0:
                        self.armed_setup[idx] = None
                        return {
                            "action": "ENTER",
                            "side": "CE",
                            "setup": f"5M_VWAP_{pat_name}",
                            "arm_time": arm["arm_time"],
                            "entry_time": bar_hhmm,
                            "spot_entry": close_px,
                            "spot_sl": arm["spot_sl"],
                            "spot_risk": round(spot_risk, 2),
                            "spot_t1": round_to_tick(close_px + spot_risk, tick_size),         # 1:1
                            "spot_t2": round_to_tick(close_px + (2.0 * spot_risk), tick_size), # 1:2
                            "entry_pcr": pcr,
                            "status": f"ENTER_CE@{bar_hhmm}",
                        }

        st = f"ARMED_{arm['side']}@{arm['arm_time']}(SL:{arm['spot_sl']:.1f})" if arm else "SCAN_5M_VWAP"
        return {"action": "NONE", "status": st}

    def manage_position_trail_and_melt(
        self,
        pos: dict,
        spot_ltp: float,
        opt_ltp: float,
        df_5m: pd.DataFrame,
        curr_pcr: float,
        hhmm: str,
    ) -> dict:
        """
        Manages:
          1. 1:1 Hit -> Shift SL to Cost-to-Cost (Break-Even)
          2. 1:2 Hit -> Shift SL to +1R Locked Profit
          3. > 2R    -> Infinity Runner trailing 20 EMA
          4. Early Exit if Momentum Stalls or PCR Melts / Option Decays
        """
        side = pos["side"]  # "PE" or "CE"
        spot_entry = float(pos["spot_entry"])
        spot_risk = max(float(pos["spot_risk"]), 1e-6)
        opt_entry = float(pos["entry_price"])
        opt_r = max(float(pos["r_unit"]), 1e-6)
        tick_sz = float(pos.get("tick_size", 0.05))
        entry_pcr = float(pos.get("entry_pcr", curr_pcr))

        # Current R-multiple achieved on underlying spot/future
        spot_move = (spot_entry - spot_ltp) if side == "PE" else (spot_ltp - spot_entry)
        curr_r = spot_move / spot_risk

        # --- STAGE 1: 1:1 (1R) -> SHIFT SL TO COST-TO-COST ---
        if curr_r >= 1.0 and pos.get("trail_stage") not in ("1R_COST_TO_COST", "2R_LOCKED_1R", "INFINITY_EMA"):
            pos["spot_sl"] = spot_entry
            pos["sl"] = round_to_tick(opt_entry + (0.05 * opt_r), tick_sz)  # Covers brokerage/taxes
            pos["trail_stage"] = "1R_COST_TO_COST"

        # --- STAGE 2: 1:2 (2R) -> SHIFT SL TO +1R LOCKED ---
        if curr_r >= 2.0 and pos.get("trail_stage") not in ("2R_LOCKED_1R", "INFINITY_EMA"):
            pos["spot_sl"] = (spot_entry - spot_risk) if side == "PE" else (spot_entry + spot_risk)
            pos["sl"] = round_to_tick(opt_entry + opt_r, tick_sz)
            pos["trail_stage"] = "2R_LOCKED_1R"

        # --- STAGE 3: INFINITY RUNNER (> 2.2R) -> TRAIL 5m 20 EMA ---
        if curr_r >= 2.2 and not df_5m.empty and len(df_5m) >= 2:
            ema20_5m = float(df_5m.iloc[-2]["ema20"])
            if side == "PE" and ema20_5m < pos["spot_sl"]:
                pos["spot_sl"] = round_to_tick(ema20_5m, tick_sz)
                pos["trail_stage"] = "INFINITY_EMA"
            elif side == "CE" and ema20_5m > pos["spot_sl"]:
                pos["spot_sl"] = round_to_tick(ema20_5m, tick_sz)
                pos["trail_stage"] = "INFINITY_EMA"

        # --- HARD STOP-LOSS CHECK (SPOT OR OPTION) ---
        if side == "PE" and (spot_ltp >= pos["spot_sl"] or opt_ltp <= pos["sl"]):
            reason = f"{pos['trail_stage']}_EXIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
            return {"exit": True, "reason": reason}

        if side == "CE" and (spot_ltp <= pos["spot_sl"] or opt_ltp <= pos["sl"]):
            reason = f"{pos['trail_stage']}_EXIT" if pos.get("trail_stage") else "STOP_LOSS_HIT"
            return {"exit": True, "reason": reason}

        # --- PCR MELT / REVERSAL EXIT ---
        # For PE: PCR rising sharply means put writers are dominating (bearish momentum melting)
        if side == "PE" and (curr_pcr - entry_pcr) >= self.pcr_melt_threshold:
            return {"exit": True, "reason": f"PCR_MELT_EXIT({entry_pcr:.2f}->{curr_pcr:.2f})"}

        # For CE: PCR dropping sharply means call writers are dominating (bullish momentum melting)
        if side == "CE" and (entry_pcr - curr_pcr) >= self.pcr_melt_threshold:
            return {"exit": True, "reason": f"PCR_MELT_EXIT({entry_pcr:.2f}->{curr_pcr:.2f})"}

        # --- NO MOMENTUM / OPTION THETA MELT EXIT ---
        if not df_5m.empty and len(df_5m) >= (self.stall_bars_limit + 2):
            recent = df_5m.iloc[-(self.stall_bars_limit + 1):-1]  # Last 3 closed 5m bars
            last_closed = recent.iloc[-1]
            avg_vol_r = float(recent["vol_ratio"].mean())
            ema20_c = float(last_closed["ema20"])
            vwap_c = float(last_closed["vwap"])
            c_px = float(last_closed["close"])

            # 1) Momentum lost: 5m candle closes back across BOTH 20 EMA and VWAP against trade
            if side == "PE" and c_px > ema20_c and c_px > vwap_c:
                return {"exit": True, "reason": "NO_MOMENTUM_EMA_VWAP_CROSS"}
            if side == "CE" and c_px < ema20_c and c_px < vwap_c:
                return {"exit": True, "reason": "NO_MOMENTUM_EMA_VWAP_CROSS"}

            # 2) Stalled price + Option premium melting (theta/IV crush) before reaching 1R
            if curr_r < 0.8 and avg_vol_r < 0.70:
                if side == "PE":
                    no_new_low = float(recent["low"].min()) > (spot_ltp - 0.10 * spot_risk)
                    opt_melting = (opt_ltp < opt_entry * 0.88) and (spot_move >= 0)
                    if no_new_low and opt_melting:
                        return {"exit": True, "reason": "MOMENTUM_AND_PREMIUM_MELT"}
                elif side == "CE":
                    no_new_high = float(recent["high"].max()) < (spot_ltp + 0.10 * spot_risk)
                    opt_melting = (opt_ltp < opt_entry * 0.88) and (spot_move >= 0)
                    if no_new_high and opt_melting:
                        return {"exit": True, "reason": "MOMENTUM_AND_PREMIUM_MELT"}

        # --- EOD EXIT ---
        if hhmm >= "15:15":
            return {"exit": True, "reason": "EOD_RUNNER_EXIT_1515"}

        return {"exit": False, "reason": None}
