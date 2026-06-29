"""Flask web application for the arbitrage bot dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, stream_with_context, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from web.audit import init_audit, log as audit_log
from web.audit import (LOGIN_OK, LOGIN_FAIL, LOGOUT, PW_CHANGE, PW_RESET,
                       USER_CREATE, USER_DELETE, SETTINGS_SAVE, BOT_START,
                       BOT_STOP, TOTP_ENABLE, TOTP_DISABLE, TOTP_FAIL)
from web.bot_manager import ROOT
from web.env_manager import read_env, write_env, get_effective_env
from web.multi_bot_manager import MultiBotManager
from web.opportunity_store import (init_opportunity_store, get_opportunities,
                                   get_stats as opp_stats, get_daily_stats,
                                   get_best_pairs, get_hourly_heatmap,
                                   get_spread_distribution, get_calendar_heatmap)

sys.path.insert(0, str(ROOT / "src"))

STORAGE_DIR = ROOT / "storage"

multi_bot = MultiBotManager()


def _user_trades_csv(user_id: int):
    p = ROOT / "data" / f"user_{user_id}" / "trades.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _user_positions(user_id: int):
    p = ROOT / "data" / f"user_{user_id}" / "positions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _classify_log(line: str) -> str:
    if "| ERROR" in line or "| CRITICAL" in line:
        return "error"
    if "| WARNING" in line:
        return "warn"
    if "| INFO" in line:
        return "info"
    return ""


def create_app() -> Flask:
    from web.auth import User, init_auth, user_count

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = _load_secret_key()
    app.jinja_env.globals["classify"] = _classify_log
    app.jinja_env.globals["enumerate"] = enumerate

    init_auth(app, STORAGE_DIR)
    init_audit(STORAGE_DIR / "audit.db")
    init_opportunity_store(ROOT / "data")

    # Auto-create admin from env vars (for Railway / fresh deploys)
    import os as _os
    _admin_user = _os.environ.get("ADMIN_USERNAME", "").strip()
    _admin_pass = _os.environ.get("ADMIN_PASSWORD", "").strip()
    if _admin_user and _admin_pass and user_count() == 0:
        User.create(_admin_user, _admin_pass, is_admin=True)

    # ------------------------------------------------------------------ #
    # Telegram always-on polling (web dashboard owns the command loop)
    # ------------------------------------------------------------------ #
    _start_telegram_polling(multi_bot, app.secret_key)

    # ------------------------------------------------------------------ #
    # Auth routes (public)
    # ------------------------------------------------------------------ #

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if user_count() > 0:
            return redirect(url_for("login"))
        if request.method == "POST":
            username  = request.form.get("username", "").strip()
            password  = request.form.get("password", "")
            password2 = request.form.get("password2", "")
            if not username or len(username) < 3:
                flash("Логин должен быть не менее 3 символов", "error")
            elif len(password) < 8:
                flash("Пароль должен быть не менее 8 символов", "error")
            elif password != password2:
                flash("Пароли не совпадают", "error")
            else:
                user = User.create(username, password, is_admin=True)
                login_user(user, remember=True)
                flash(f"Аккаунт '{username}' создан. Добро пожаловать!", "success")
                return redirect(url_for("index"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.get_by_username(username)
            ip = request.remote_addr
            if user and user.check_password(password):
                if user.totp_enabled:
                    from flask import session
                    session["_2fa_uid"] = user.id
                    return redirect(url_for("two_factor_verify"))
                login_user(user, remember=True)
                User.record_login(user.id)
                audit_log(LOGIN_OK, user_id=user.id, username=user.username, ip=ip)
                return redirect(request.args.get("next") or url_for("index"))
            audit_log(LOGIN_FAIL, username=username, details="wrong password", ip=ip)
            flash("Неверный логин или пароль", "error")
        return render_template("login.html", show_register=(user_count() == 0))

    @app.route("/logout")
    @login_required
    def logout():
        audit_log(LOGOUT, user_id=current_user.id, username=current_user.username,
                  ip=request.remote_addr)
        logout_user()
        return redirect(url_for("login"))

    # ------------------------------------------------------------------ #
    # Pages (protected)
    # ------------------------------------------------------------------ #

    @app.route("/")
    @login_required
    def index():
        env = read_env()
        exchanges = _exchange_status(env)
        positions = _load_positions(current_user.id)
        logs = multi_bot.tail_log(current_user.id, 150)
        return render_template(
            "index.html",
            is_running=multi_bot.is_running(current_user.id),
            pid=multi_bot.pid(current_user.id),
            exchanges=exchanges,
            positions=positions,
            logs=logs,
            env=env,
            current_user=current_user,
        )

    @app.route("/settings")
    @login_required
    def settings():
        env = read_env()
        return render_template("settings.html", env=env, current_user=current_user)

    @app.route("/docs")
    @login_required
    def docs():
        return render_template("docs.html", current_user=current_user)

    @app.route("/profitability")
    @login_required
    def profitability():
        KALSHI_FEE  = 0.07
        POLY_FEE    = 0.02
        AMM_BUFFER  = 0.02

        def _economics(yes_ask: float, no_ask_raw: float):
            no_eff = no_ask_raw + AMM_BUFFER
            total  = yes_ask + no_eff
            gross  = 1.0 - total
            if gross <= 0:
                return 0.0, 0.0, 0.0
            fee_yes_wins = KALSHI_FEE * (1.0 - yes_ask)
            fee_no_wins  = POLY_FEE   * (1.0 - no_eff)
            worst_fee    = max(fee_yes_wins, fee_no_wins)
            net = gross - worst_fee
            return round(gross * 100, 2), round(worst_fee * 100, 2), round(max(net, 0.0) * 100, 2)

        raw_scenarios = [
            {
                "name": "Пессимистичный",
                "color": "danger",
                "icon": "shield-alt",
                "yes_ask": 0.46, "no_ask": 0.44,
                "trades_per_year": 8,
                "success_rate": 0.60,
                "avg_hold_days": 45,
                "desc": "Рынок эффективен. Небольшой спред, частые несрабатывания.",
            },
            {
                "name": "Реалистичный",
                "color": "warning",
                "icon": "balance-scale",
                "yes_ask": 0.43, "no_ask": 0.40,
                "trades_per_year": 12,
                "success_rate": 0.70,
                "avg_hold_days": 30,
                "desc": "Умеренные мисценки, регулярный поиск. Базовый сценарий.",
            },
            {
                "name": "Оптимистичный",
                "color": "success",
                "icon": "rocket",
                "yes_ask": 0.38, "no_ask": 0.35,
                "trades_per_year": 18,
                "success_rate": 0.80,
                "avg_hold_days": 20,
                "desc": "Активный рынок. Крупные мисценки, быстрое auto-close.",
            },
        ]

        START = 10.0
        scenarios = []

        for sc in raw_scenarios:
            gross_pct, fee_pct, net_pct = _economics(sc["yes_ask"], sc["no_ask"])
            trades_pm = sc["trades_per_year"] / 12.0

            months = []
            capital = START
            total_gross = 0.0
            total_fees  = 0.0
            total_net   = 0.0

            for m in range(1, 13):
                successful   = trades_pm * sc["success_rate"]
                gross_dollar = capital * successful * (gross_pct / 100.0)
                fee_dollar   = capital * successful * (fee_pct  / 100.0)
                net_dollar   = capital * successful * (net_pct  / 100.0)
                capital     += net_dollar
                total_gross += gross_dollar
                total_fees  += fee_dollar
                total_net   += net_dollar
                months.append({
                    "month": m,
                    "trades": round(trades_pm, 1),
                    "successful": round(successful, 2),
                    "gross": round(gross_dollar, 3),
                    "fees": round(fee_dollar, 3),
                    "net": round(net_dollar, 3),
                    "capital": round(capital, 3),
                    "roi": round((capital / START - 1) * 100, 1),
                })

            scenarios.append({
                **sc,
                "gross_pct": gross_pct,
                "fee_pct": fee_pct,
                "net_pct": net_pct,
                "months": months,
                "final_capital": round(capital, 2),
                "total_profit": round(capital - START, 2),
                "annual_roi": round((capital / START - 1) * 100, 1),
                "total_gross": round(total_gross, 2),
                "total_fees": round(total_fees, 2),
                "total_net": round(total_net, 2),
            })

        return render_template(
            "profitability.html",
            scenarios=scenarios,
            start=START,
            kalshi_fee=KALSHI_FEE * 100,
            poly_fee=POLY_FEE * 100,
            amm_buffer=AMM_BUFFER * 100,
            current_user=current_user,
        )

    @app.route("/api/opportunities")
    @login_required
    def api_opportunities():
        opp_file = ROOT / "data" / "live_opportunities.json"
        if not opp_file.is_file():
            return jsonify({"ts": None, "iteration": 0, "checked_pairs": 0, "opportunities": []})
        try:
            return jsonify(json.loads(opp_file.read_text(encoding="utf-8")))
        except Exception:
            return jsonify({"ts": None, "iteration": 0, "checked_pairs": 0, "opportunities": []})

    # ------------------------------------------------------------------ #
    # User management (admin only)
    # ------------------------------------------------------------------ #

    @app.route("/users")
    @login_required
    def users():
        if not current_user.is_admin:
            flash("Только администраторы могут управлять пользователями", "error")
            return redirect(url_for("index"))
        all_users = User.all_users()
        return render_template("users.html", users=all_users, current_user=current_user)

    @app.route("/users/add", methods=["POST"])
    @login_required
    def users_add():
        if not current_user.is_admin:
            return redirect(url_for("index"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role     = request.form.get("role", "user")
        if not username or len(username) < 3:
            flash("Логин должен быть не менее 3 символов", "error")
        elif len(password) < 8:
            flash("Пароль должен быть не менее 8 символов", "error")
        elif User.get_by_username(username):
            flash(f"Пользователь '{username}' уже существует", "error")
        else:
            User.create(username, password, is_admin=(role == "admin"))
            audit_log(USER_CREATE, user_id=current_user.id, username=current_user.username,
                      details=f"created={username} role={role}", ip=request.remote_addr)
            flash(f"Пользователь '{username}' добавлен", "success")
        return redirect(url_for("users"))

    @app.route("/users/delete/<int:uid>", methods=["POST"])
    @login_required
    def users_delete(uid: int):
        if not current_user.is_admin:
            return redirect(url_for("index"))
        if uid == current_user.id:
            flash("Нельзя удалить самого себя", "error")
        else:
            User.delete(uid)
            audit_log(USER_DELETE, user_id=current_user.id, username=current_user.username,
                      details=f"deleted_uid={uid}", ip=request.remote_addr)
            flash("Пользователь удалён", "success")
        return redirect(url_for("users"))

    @app.route("/users/reset-password/<int:uid>", methods=["POST"])
    @login_required
    def users_reset_password(uid: int):
        if not current_user.is_admin:
            return redirect(url_for("index"))
        new_pw  = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new_pw) < 8:
            flash("Пароль должен быть не менее 8 символов", "error")
        elif new_pw != confirm:
            flash("Пароли не совпадают", "error")
        else:
            target = User.get_by_id(uid)
            if target:
                User.set_password(uid, new_pw)
                audit_log(PW_RESET, user_id=current_user.id, username=current_user.username,
                          details=f"reset_for={target.username}", ip=request.remote_addr)
                flash(f"Пароль пользователя «{target.username}» изменён", "success")
            else:
                flash("Пользователь не найден", "error")
        return redirect(url_for("users"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            old_pw   = request.form.get("old_password", "")
            new_pw   = request.form.get("new_password", "")
            confirm  = request.form.get("confirm_password", "")
            if not current_user.check_password(old_pw):
                flash("Текущий пароль неверный", "error")
            elif len(new_pw) < 8:
                flash("Новый пароль должен быть не менее 8 символов", "error")
            elif new_pw != confirm:
                flash("Пароли не совпадают", "error")
            elif new_pw == old_pw:
                flash("Новый пароль совпадает со старым", "error")
            else:
                User.set_password(current_user.id, new_pw)
                audit_log(PW_CHANGE, user_id=current_user.id, username=current_user.username,
                          ip=request.remote_addr)
                flash("Пароль успешно изменён", "success")
                return redirect(url_for("index"))
        return render_template("change_password.html", current_user=current_user)

    # ------------------------------------------------------------------ #
    # 2FA (TOTP)
    # ------------------------------------------------------------------ #

    @app.route("/2fa/verify", methods=["GET", "POST"])
    def two_factor_verify():
        from flask import session
        uid = session.get("_2fa_uid")
        if not uid:
            return redirect(url_for("login"))
        user = User.get_by_id(uid)
        if not user:
            session.pop("_2fa_uid", None)
            return redirect(url_for("login"))
        if request.method == "POST":
            code = request.form.get("code", "").strip()
            if user.verify_totp(code):
                session.pop("_2fa_uid", None)
                login_user(user, remember=True)
                User.record_login(user.id)
                audit_log(LOGIN_OK, user_id=user.id, username=user.username,
                          details="2fa_ok", ip=request.remote_addr)
                return redirect(url_for("index"))
            audit_log(TOTP_FAIL, user_id=user.id, username=user.username,
                      ip=request.remote_addr)
            flash("Неверный код. Попробуйте ещё раз.", "error")
        return render_template("two_factor_verify.html", username=user.username)

    @app.route("/2fa/setup", methods=["GET", "POST"])
    @login_required
    def two_factor_setup():
        import pyotp
        from flask import session
        if request.method == "POST":
            action = request.form.get("action")
            if action == "enable":
                secret = session.get("_2fa_new_secret")
                code   = request.form.get("code", "").strip()
                if not secret:
                    flash("Сессия истекла, начните заново", "error")
                    return redirect(url_for("two_factor_setup"))
                totp = pyotp.TOTP(secret)
                if totp.verify(code, valid_window=1):
                    User.set_totp(current_user.id, secret, True)
                    session.pop("_2fa_new_secret", None)
                    audit_log(TOTP_ENABLE, user_id=current_user.id,
                              username=current_user.username, ip=request.remote_addr)
                    flash("Двухфакторная аутентификация включена!", "success")
                    return redirect(url_for("index"))
                flash("Неверный код — проверь время на телефоне", "error")
            elif action == "disable":
                code = request.form.get("code", "").strip()
                user = User.get_by_id(current_user.id)
                if user and user.verify_totp(code):
                    User.set_totp(current_user.id, "", False)
                    audit_log(TOTP_DISABLE, user_id=current_user.id,
                              username=current_user.username, ip=request.remote_addr)
                    flash("2FA отключена", "success")
                    return redirect(url_for("index"))
                flash("Неверный код", "error")

        # GET — generate new secret for setup
        user = User.get_by_id(current_user.id)
        if user and user.totp_enabled:
            return render_template("two_factor_setup.html", enabled=True, current_user=current_user)

        secret = pyotp.random_base32()
        session["_2fa_new_secret"] = secret
        totp = pyotp.TOTP(secret)
        otp_uri = totp.provisioning_uri(
            name=current_user.username,
            issuer_name="ArbitrageBot",
        )
        return render_template("two_factor_setup.html", enabled=False,
                               secret=secret, otp_uri=otp_uri, current_user=current_user)

    # ------------------------------------------------------------------ #
    # Audit log (admin only)
    # ------------------------------------------------------------------ #

    @app.route("/audit")
    @login_required
    def audit():
        if not current_user.is_admin:
            flash("Только администраторы могут просматривать журнал", "error")
            return redirect(url_for("index"))
        from web.audit import get_log
        entries = get_log(limit=300)
        return render_template("audit.html", entries=entries, current_user=current_user)

    # ------------------------------------------------------------------ #
    # Opportunity history
    # ------------------------------------------------------------------ #

    @app.route("/opportunities")
    @login_required
    def opportunities():
        min_pct = float(request.args.get("min_pct", 0.0))
        date    = request.args.get("date", "")
        opps    = get_opportunities(limit=300, min_pct=min_pct, date=date or None)
        stats   = opp_stats()
        daily   = get_daily_stats(30)
        return render_template("opportunities.html", opps=opps, stats=stats,
                               daily=daily, min_pct=min_pct, date=date,
                               current_user=current_user)

    # ------------------------------------------------------------------ #
    # Health check (public, for uptime monitors)
    # ------------------------------------------------------------------ #

    @app.route("/health")
    def health():
        from web.auth import user_count
        db_ok = True
        try:
            user_count()
        except Exception:
            db_ok = False
        return jsonify({
            "ok":      db_ok and True,
            "bot":     multi_bot.is_running(current_user.id) if current_user.is_authenticated else False,
            "db":      db_ok,
            "version": "1.0.0",
        }), 200 if db_ok else 503

    # ------------------------------------------------------------------ #
    # Bot control API (protected)
    # ------------------------------------------------------------------ #

    @app.route("/api/telegram/test", methods=["POST"])
    @login_required
    def api_telegram_test():
        env = read_env()
        token   = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы"})
        try:
            import httpx as _httpx
            r = _httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ ArbitrageBot: тест уведомлений работает!"},
                timeout=10.0,
            )
            data = r.json()
            if data.get("ok"):
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": data.get("description", "unknown")})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/start", methods=["POST"])
    @login_required
    def api_start():
        user_env = get_effective_env(current_user.id, app.secret_key)
        ok, msg = multi_bot.start(current_user.id, user_env)
        if ok:
            audit_log(BOT_START, user_id=current_user.id, username=current_user.username,
                      ip=request.remote_addr)
        return jsonify({"ok": ok, "message": msg,
                        "running": multi_bot.is_running(current_user.id),
                        "pid": multi_bot.pid(current_user.id)})

    @app.route("/api/stop", methods=["POST"])
    @login_required
    def api_stop():
        ok, msg = multi_bot.stop(current_user.id)
        if ok:
            audit_log(BOT_STOP, user_id=current_user.id, username=current_user.username,
                      ip=request.remote_addr)
        return jsonify({"ok": ok, "message": msg, "running": multi_bot.is_running(current_user.id)})

    @app.route("/api/status")
    @login_required
    def api_status():
        env = get_effective_env(current_user.id, app.secret_key)
        positions = _load_positions(current_user.id)
        return jsonify({
            "running": multi_bot.is_running(current_user.id),
            "pid": multi_bot.pid(current_user.id),
            "dry_run": env.get("DRY_RUN", "true").lower() == "true",
            "exchanges": _exchange_status(env),
            "open_positions": positions.get("open_count", 0),
            "daily_pnl": positions.get("daily_pnl", 0.0),
        })

    @app.route("/api/logs")
    @login_required
    def api_logs():
        lines = multi_bot.tail_log(current_user.id, int(request.args.get("n", 200)))
        return jsonify({"lines": lines})

    @app.route("/api/trades")
    @login_required
    def api_trades():
        n = int(request.args.get("n", 50))
        try:
            from core.trade_log import TradeLogger
            tl = TradeLogger(_user_trades_csv(current_user.id))
            return jsonify({"trades": tl.recent(n), "summary": tl.summary()})
        except Exception:
            return jsonify({"trades": [], "summary": {}})

    @app.route("/api/pnl-history")
    @login_required
    def api_pnl_history():
        pnl_file = multi_bot.data_dir(current_user.id) / "pnl_history.json"
        if not pnl_file.is_file():
            return jsonify({"history": []})
        try:
            raw: dict = json.loads(pnl_file.read_text(encoding="utf-8"))
            items = sorted(raw.items())[-30:]
            return jsonify({"history": [{"date": d, "pnl": v} for d, v in items]})
        except Exception:
            return jsonify({"history": []})

    @app.route("/api/logs/stream")
    @login_required
    def api_logs_stream():
        uid = current_user.id
        def generate():
            for line in multi_bot.stream_log(uid):
                yield f"data: {json.dumps(line)}\n\n"
        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/settings", methods=["POST"])
    @login_required
    def api_save_settings():
        if not current_user.is_admin:
            return jsonify({"ok": False, "error": "Нет прав"})
        data: dict = request.json or {}
        BOOL_FIELDS = {"DRY_RUN", "KELLY_ENABLED", "SEMANTIC_MATCHING_ENABLED"}
        allowed = {
            "DRY_RUN", "MIN_PROFIT_PERCENT", "MIN_LEG_LIQUIDITY",
            "POLL_INTERVAL_SECONDS", "MAX_DAILY_LOSS_PCT", "MAX_DAILY_LOSS_USD",
            "KELLY_ENABLED", "KELLY_FRACTION", "KELLY_BANKROLL",
            "MARKET_CACHE_TTL_SECONDS", "SEMANTIC_MATCHING_ENABLED",
            "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_PATH",
            "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
            "POLYMARKET_PRIVATE_KEY",
            "BETFAIR_USERNAME", "BETFAIR_PASSWORD", "BETFAIR_APP_KEY",
            "SMARKETS_API_TOKEN",
            "BETDAQ_USERNAME", "BETDAQ_PASSWORD", "BETDAQ_API_KEY",
            "MATCHBOOK_USERNAME", "MATCHBOOK_PASSWORD",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        }
        updates = {}
        for k, v in data.items():
            if k not in allowed:
                continue
            sv = str(v).strip()
            if k in BOOL_FIELDS:
                updates[k] = "true" if sv.lower() in ("true", "1", "on", "yes") else "false"
            elif sv:
                updates[k] = sv
        write_env(updates)
        audit_log(SETTINGS_SAVE, user_id=current_user.id, username=current_user.username,
                  details=", ".join(updates.keys()), ip=request.remote_addr)
        return jsonify({"ok": True, "saved": list(updates.keys())})

    # ------------------------------------------------------------------ #
    # Per-user API keys
    # ------------------------------------------------------------------ #

    @app.route("/api/user/keys", methods=["GET"])
    @login_required
    def api_user_keys_get():
        from web.auth import UserApiKey
        names = UserApiKey.get_names(current_user.id)
        return jsonify({"ok": True, "keys": names})

    @app.route("/api/user/keys", methods=["POST"])
    @login_required
    def api_user_keys_save():
        from web.auth import UserApiKey
        data: dict = request.json or {}
        saved = []
        for key_name, value in data.items():
            if key_name not in UserApiKey.API_KEY_NAMES:
                continue
            v = str(value).strip()
            if v:
                UserApiKey.set(current_user.id, key_name, v, app.secret_key)
                saved.append(key_name)
        return jsonify({"ok": True, "saved": saved})

    @app.route("/api/user/keys/<key_name>", methods=["DELETE"])
    @login_required
    def api_user_keys_delete(key_name: str):
        from web.auth import UserApiKey
        if key_name not in UserApiKey.API_KEY_NAMES:
            return jsonify({"ok": False, "error": "unknown key"})
        UserApiKey.delete(current_user.id, key_name)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # P&L dashboard
    # ------------------------------------------------------------------ #

    @app.route("/pnl")
    @login_required
    def pnl():
        from core.trade_log import TradeLogger
        trades_csv = _user_trades_csv(current_user.id)
        tl = TradeLogger(trades_csv)
        trades   = tl.recent(500)
        summary  = tl.summary()
        pnl_file = multi_bot.data_dir(current_user.id) / "pnl_history.json"
        pnl_history: list[dict] = []
        if pnl_file.is_file():
            try:
                raw = json.loads(pnl_file.read_text(encoding="utf-8"))
                pnl_history = [{"date": d, "pnl": v} for d, v in sorted(raw.items())[-60:]]
            except Exception:
                pass
        return render_template("pnl.html", trades=trades, summary=summary,
                               pnl_history=pnl_history, current_user=current_user)

    @app.route("/pnl/download")
    @login_required
    def pnl_download():
        from flask import send_file
        trades_csv = _user_trades_csv(current_user.id)
        if trades_csv.is_file():
            return send_file(trades_csv, mimetype="text/csv",
                             as_attachment=True, download_name="trades.csv")
        return "Нет данных", 404

    # ------------------------------------------------------------------ #
    # Backtest UI
    # ------------------------------------------------------------------ #

    @app.route("/backtest", methods=["GET", "POST"])
    @login_required
    def backtest():
        result = None
        error  = None
        if request.method == "POST":
            mode = request.form.get("mode", "live")
            stake = float(request.form.get("stake", 10.0) or 10.0)
            if mode == "live":
                try:
                    from config.settings import Settings
                    from backtest.runner import run_live_snapshot
                    settings = Settings()
                    # Try to build real clients; fall back to empty dict (shows 0 opps gracefully)
                    clients: dict = {}
                    try:
                        import importlib
                        br_mod = importlib.import_module("run")
                        clients = br_mod._build_clients(settings)
                    except Exception:
                        pass
                    br = run_live_snapshot(settings, clients, stake_usd=stake)
                    result = {"summary": br.summary(), "opportunities": br.opportunities, "mode": "live"}
                except Exception as e:
                    error = str(e)
            elif mode == "csv":
                f = request.files.get("csv_file")
                if f and f.filename:
                    import tempfile, os
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
                    f.save(tmp.name)
                    try:
                        from config.settings import Settings
                        from backtest.runner import run_csv_replay
                        br = run_csv_replay(Path(tmp.name), Settings(), stake_usd=stake)
                        result = {"summary": br.summary(), "opportunities": br.opportunities, "mode": "csv"}
                    except Exception as e:
                        error = str(e)
                    finally:
                        os.unlink(tmp.name)
                else:
                    error = "Файл не выбран"
        return render_template("backtest.html", result=result, error=error, current_user=current_user)

    # ------------------------------------------------------------------ #
    # Export endpoints
    # ------------------------------------------------------------------ #

    @app.route("/audit/download")
    @login_required
    def audit_download():
        if not current_user.is_admin:
            return "Нет прав", 403
        from web.audit import get_log
        import io, csv as csv_mod
        entries = get_log(limit=10000)
        output = io.StringIO()
        w = csv_mod.DictWriter(output, fieldnames=["ts","user_id","username","action","details","ip"])
        w.writeheader()
        w.writerows(entries)
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=audit_log.csv"})

    @app.route("/opportunities/download")
    @login_required
    def opportunities_download():
        min_pct = float(request.args.get("min_pct", 0.0))
        date    = request.args.get("date", "") or None
        opps    = get_opportunities(limit=10000, min_pct=min_pct, date=date)
        import io, csv as csv_mod
        output = io.StringIO()
        fields = ["id","ts","date","title","yes_venue","no_venue","yes_price","no_price","profit_pct","max_size","executed"]
        w = csv_mod.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(opps)
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=opportunities.csv"})

    # ------------------------------------------------------------------ #
    # Analytics
    # ------------------------------------------------------------------ #

    @app.route("/analytics")
    @login_required
    def analytics():
        return render_template(
            "analytics.html",
            best_pairs   = get_best_pairs(25),
            heatmap      = get_hourly_heatmap(),
            distribution = get_spread_distribution(),
            calendar     = get_calendar_heatmap(365),
            stats        = opp_stats(),
            current_user = current_user,
        )

    # ------------------------------------------------------------------ #
    # Markets page (current monitoring state)
    # ------------------------------------------------------------------ #

    @app.route("/markets")
    @login_required
    def markets():
        live_file = ROOT / "data" / "live_opportunities.json"
        live: dict = {}
        if live_file.is_file():
            try:
                live = json.loads(live_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return render_template("markets.html", live=live,
                               is_running=multi_bot.is_running(current_user.id), current_user=current_user)

    # ------------------------------------------------------------------ #
    # Snapshot recorder (saves current live_opportunities to CSV for backtest)
    # ------------------------------------------------------------------ #

    @app.route("/api/snapshot", methods=["POST"])
    @login_required
    def api_snapshot():
        live_file = ROOT / "data" / "live_opportunities.json"
        if not live_file.is_file():
            return jsonify({"ok": False, "error": "Нет данных — запусти бота"})
        try:
            data = json.loads(live_file.read_text(encoding="utf-8"))
        except Exception:
            return jsonify({"ok": False, "error": "Ошибка чтения файла"})

        import csv as csv_mod
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        snap_dir = ROOT / "data" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        fname = f"snapshot_{ts[:19].replace(':','-')}.csv"
        snap_path = snap_dir / fname

        opps = data.get("opportunities", [])
        rows = []
        for o in opps:
            rows.append({
                "timestamp_utc": ts,
                "venue":         o.get("yes_venue", ""),
                "market_id":     o.get("title", "")[:60],
                "title":         o.get("title", ""),
                "yes_best_ask":  o.get("yes_price", ""),
                "no_best_ask":   o.get("no_price", ""),
            })
        with open(snap_path, "w", newline="", encoding="utf-8") as f:
            w = csv_mod.DictWriter(f, fieldnames=["timestamp_utc","venue","market_id","title","yes_best_ask","no_best_ask"])
            w.writeheader()
            w.writerows(rows)
        return jsonify({"ok": True, "file": fname, "rows": len(rows)})

    @app.route("/api/snapshots")
    @login_required
    def api_snapshots():
        snap_dir = ROOT / "data" / "snapshots"
        if not snap_dir.is_dir():
            return jsonify({"files": []})
        files = sorted(snap_dir.glob("*.csv"), reverse=True)
        return jsonify({"files": [f.name for f in files[:20]]})

    @app.route("/api/snapshots/<fname>")
    @login_required
    def api_snapshot_download(fname: str):
        import re
        if not re.match(r'^snapshot_[\w\-]+\.csv$', fname):
            return "Invalid filename", 400
        from flask import send_file
        snap_path = ROOT / "data" / "snapshots" / fname
        if not snap_path.is_file():
            return "Not found", 404
        return send_file(snap_path, mimetype="text/csv",
                         as_attachment=True, download_name=fname)

    # ------------------------------------------------------------------ #
    # Validate / system check
    # ------------------------------------------------------------------ #

    @app.route("/validate")
    @login_required
    def validate():
        import socket
        env = read_env()
        checks = []

        def _check(name, ok, detail=""):
            checks.append({"name": name, "ok": ok, "detail": detail})

        # DB
        try:
            from web.auth import user_count
            user_count()
            _check("База данных пользователей", True, "storage/users.db доступна")
        except Exception as e:
            _check("База данных пользователей", False, str(e))

        # opportunities DB
        try:
            opp_stats()
            _check("База возможностей", True, "data/opportunities.db доступна")
        except Exception as e:
            _check("База возможностей", False, str(e))

        # .env file
        env_path = ROOT / ".env"
        _check("Файл .env", env_path.is_file(), str(env_path))

        # Kalshi credentials
        has_kalshi = bool(env.get("KALSHI_API_KEY_ID", "").strip())
        _check("Kalshi API Key", has_kalshi,
               "KALSHI_API_KEY_ID задан" if has_kalshi else "Не задан — торговля на Kalshi невозможна")

        # Polymarket
        has_poly = bool(env.get("POLYMARKET_API_KEY", "").strip())
        _check("Polymarket API Key", has_poly,
               "POLYMARKET_API_KEY задан" if has_poly else "Не задан")

        # Telegram
        has_tg = bool(env.get("TELEGRAM_BOT_TOKEN","").strip() and env.get("TELEGRAM_CHAT_ID","").strip())
        _check("Telegram уведомления", has_tg,
               "Токен и Chat ID заданы" if has_tg else "Не настроен")

        # Network — Kalshi
        try:
            socket.setdefaulttimeout(5)
            socket.getaddrinfo("api.elections.kalshi.com", 443)
            _check("Сеть → Kalshi", True, "DNS resolves")
        except Exception as e:
            _check("Сеть → Kalshi", False, str(e))

        # Network — Polymarket
        try:
            socket.getaddrinfo("clob.polymarket.com", 443)
            _check("Сеть → Polymarket", True, "DNS resolves")
        except Exception as e:
            _check("Сеть → Polymarket", False, str(e))

        # DRY_RUN
        dry = env.get("DRY_RUN", "true").lower() == "true"
        _check("Режим DRY_RUN", not dry,
               "Выключен — реальная торговля" if not dry else "Включён — сделки не исполняются")

        # Log file
        log_ok = (ROOT / "logs" / "bot.log").is_file()
        _check("Лог-файл бота", log_ok,
               "logs/bot.log существует" if log_ok else "Ещё не создан — запусти бота")

        return render_template("validate.html", checks=checks, current_user=current_user)

    # ------------------------------------------------------------------ #
    # Autostart generator (launchd plist for macOS)
    # ------------------------------------------------------------------ #

    @app.route("/autostart")
    @login_required
    def autostart():
        if not current_user.is_admin:
            flash("Только администраторы", "error")
            return redirect(url_for("index"))
        import shutil
        venv_py = Path("/Users/mac/PycharmProjects/spred/.venv/bin/python3")
        if not venv_py.is_file():
            venv_py = ROOT / ".venv" / "bin" / "python3"
        python_bin = str(venv_py) if venv_py.is_file() else (shutil.which("python3") or "/usr/bin/python3")

        plist_bot = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.arbitragebot.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{ROOT / 'run.py'}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{ROOT}/logs/bot.log</string>
    <key>StandardErrorPath</key>
    <string>{ROOT}/logs/bot.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>"""
        plist_name = "com.arbitragebot.bot.plist"
        plist_path = Path.home() / "Library" / "LaunchAgents" / plist_name

        return render_template("autostart.html",
                               plist=plist_bot, plist_name=plist_name,
                               plist_path=str(plist_path), root=str(ROOT),
                               python_bin=python_bin,
                               current_user=current_user)

    @app.route("/autostart/install", methods=["POST"])
    @login_required
    def autostart_install():
        if not current_user.is_admin:
            return jsonify({"ok": False, "error": "Нет прав"})
        plist_content = request.form.get("plist", "")
        plist_name    = request.form.get("plist_name", "com.arbitragebot.web.plist")
        plist_dir     = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / plist_name
        try:
            plist_path.write_text(plist_content, encoding="utf-8")
            flash(f"Сохранён: {plist_path}. Выполни: launchctl load {plist_path}", "success")
        except Exception as e:
            flash(f"Ошибка записи: {e}", "error")
        return redirect(url_for("autostart"))

    # ------------------------------------------------------------------ #
    # Log archive viewer
    # ------------------------------------------------------------------ #

    @app.route("/logs")
    @login_required
    def logs_viewer():
        log_dir = ROOT / "logs"
        log_files = []
        if log_dir.is_dir():
            for f in sorted(log_dir.glob("*.log"), reverse=True):
                log_files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                })
        return render_template("logs_viewer.html", log_files=log_files, current_user=current_user)

    @app.route("/api/logs/file")
    @login_required
    def api_log_file():
        name = request.args.get("name", "bot.log")
        import re
        if not re.match(r'^[\w.\-]+\.log$', name):
            return jsonify({"lines": [], "error": "Invalid filename"})
        log_path = ROOT / "logs" / name
        if not log_path.is_file():
            return jsonify({"lines": []})
        n = int(request.args.get("n", 300))
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()[-n:]
            return jsonify({"lines": lines, "total": len(text.splitlines())})
        except Exception as e:
            return jsonify({"lines": [], "error": str(e)})

    # ------------------------------------------------------------------ #
    # Security: brute-force protection + session timeout
    # ------------------------------------------------------------------ #

    from datetime import timedelta
    app.permanent_session_lifetime = timedelta(hours=12)

    _login_attempts: dict = {}  # ip -> [timestamps]

    def _is_rate_limited(ip: str) -> bool:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 900]  # 15-min window
        _login_attempts[ip] = attempts
        return len(attempts) >= 5

    def _record_attempt(ip: str) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        _login_attempts.setdefault(ip, []).append(now)

    def _clear_attempts(ip: str) -> None:
        _login_attempts.pop(ip, None)

    app.jinja_env.globals["_is_rate_limited"] = _is_rate_limited
    app.jinja_env.globals["_record_attempt"]  = _record_attempt
    app.jinja_env.globals["_clear_attempts"]  = _clear_attempts

    # Patch login route to apply rate limiting
    original_login = app.view_functions.get("login")

    def login_with_ratelimit():
        ip = request.remote_addr or "unknown"
        if request.method == "POST":
            if _is_rate_limited(ip):
                flash("Слишком много попыток. Подождите 15 минут.", "error")
                return redirect(url_for("login"))
        resp = original_login()
        # If it was a failed POST, record the attempt
        if request.method == "POST":
            from web.auth import User as _User
            username = request.form.get("username", "")
            user = _User.get_by_username(username)
            if not user or not user.check_password(request.form.get("password", "")):
                _record_attempt(ip)
            else:
                _clear_attempts(ip)
        return resp

    app.view_functions["login"] = login_with_ratelimit

    # Session timeout: mark sessions permanent so lifetime applies
    @app.before_request
    def _make_session_permanent():
        from flask import session
        session.permanent = True

    # IP allowlist
    _ALLOWLIST_ENV = "DASHBOARD_IP_ALLOWLIST"

    @app.before_request
    def _ip_allowlist():
        allowlist_raw = read_env().get(_ALLOWLIST_ENV, "").strip()
        if not allowlist_raw:
            return
        allowed_ips = [x.strip() for x in allowlist_raw.split(",") if x.strip()]
        if not allowed_ips:
            return
        public_eps = {"login", "setup", "health", "static"}
        if request.endpoint in public_eps:
            return
        if request.remote_addr not in allowed_ips:
            return jsonify({"error": "Access denied"}), 403

    # Redirect root to login if no users
    @app.before_request
    def _first_run_check():
        from web.auth import user_count as uc
        public = {"setup", "login", "static"}
        if request.endpoint in public:
            return
        if uc() == 0:
            return redirect(url_for("setup"))

    return app


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _load_secret_key() -> str:
    key_file = STORAGE_DIR / "secret.key"
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if key_file.is_file():
        return key_file.read_text().strip()
    import secrets
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key


def _exchange_status(env: dict) -> list[dict]:
    return [
        {"name": "Polymarket", "key": "polymarket", "configured": True, "note": "Публичные данные"},
        {"name": "Kalshi",     "key": "kalshi",
         "configured": bool(env.get("KALSHI_API_KEY_ID")),
         "note": env.get("KALSHI_API_KEY_ID", "нет ключа")},
        {"name": "Betfair",    "key": "betfair",
         "configured": bool(env.get("BETFAIR_USERNAME") and env.get("BETFAIR_APP_KEY")),
         "note": env.get("BETFAIR_USERNAME", "нет логина")},
        {"name": "Smarkets",   "key": "smarkets",
         "configured": bool(env.get("SMARKETS_API_TOKEN")),
         "note": "токен" if env.get("SMARKETS_API_TOKEN") else "нет токена"},
        {"name": "Betdaq",     "key": "betdaq",
         "configured": bool(env.get("BETDAQ_USERNAME") and env.get("BETDAQ_API_KEY")),
         "note": env.get("BETDAQ_USERNAME", "нет логина")},
        {"name": "Matchbook",  "key": "matchbook",
         "configured": bool(env.get("MATCHBOOK_USERNAME")),
         "note": env.get("MATCHBOOK_USERNAME", "нет логина")},
    ]


def _load_positions(user_id: int = 0) -> dict:
    daily_pnl = _read_today_pnl(user_id)
    pos_file = _user_positions(user_id) if user_id else ROOT / "data" / "positions.json"
    if not pos_file.is_file():
        return {"open_count": 0, "positions": [], "daily_pnl": daily_pnl}
    try:
        data = json.loads(pos_file.read_text(encoding="utf-8"))
        return {
            "open_count": data.get("open_count", 0),
            "positions":  data.get("positions", []),
            "daily_pnl":  daily_pnl,
        }
    except Exception:
        return {"open_count": 0, "positions": [], "daily_pnl": daily_pnl}


def _read_today_pnl(user_id: int = 0) -> float:
    pnl_file = (multi_bot.data_dir(user_id) if user_id else ROOT / "data") / "pnl_history.json"
    if not pnl_file.is_file():
        return 0.0
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        data: dict = json.loads(pnl_file.read_text(encoding="utf-8"))
        return float(data.get(today, 0.0))
    except Exception:
        return 0.0


# ------------------------------------------------------------------ #
# Telegram always-on polling
# ------------------------------------------------------------------ #

_tg_polling_started = False


def _start_telegram_polling(bot_mgr, app_secret: str = "") -> None:
    """Start a single long-poll thread for Telegram commands.
    Runs in the web-dashboard process so commands work even when the
    arbitrage engine is stopped."""
    global _tg_polling_started
    if _tg_polling_started:
        return
    _tg_polling_started = True

    import threading, time
    import httpx as _httpx

    _app_secret = app_secret

    def _poll():
        env = read_env()
        token   = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return  # not configured

        url_updates  = f"https://api.telegram.org/bot{token}/getUpdates"
        url_send     = f"https://api.telegram.org/bot{token}/sendMessage"
        offset = 0

        def send(text: str) -> None:
            try:
                _httpx.post(url_send, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                }, timeout=10.0)
            except Exception:
                pass

        DASHBOARD_URL = env.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if DASHBOARD_URL:
            DASHBOARD_URL = f"https://{DASHBOARD_URL}"
        else:
            DASHBOARD_URL = "https://web-production-a4120.up.railway.app"

        MAIN_KEYBOARD = {
            "inline_keyboard": [
                [
                    {"text": "📊 Статус",       "callback_data": "status"},
                    {"text": "💰 P&L",           "callback_data": "pnl"},
                ],
                [
                    {"text": "▶️ Запустить бот", "callback_data": "run"},
                    {"text": "⏹ Остановить",     "callback_data": "stop"},
                ],
                [
                    {"text": "📈 Возможности",   "callback_data": "opps"},
                    {"text": "🌐 Дашборд",       "url": DASHBOARD_URL},
                ],
            ]
        }

        def send_keyboard(text: str, keyboard: dict = None) -> None:
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
            if keyboard:
                payload["reply_markup"] = keyboard
            try:
                _httpx.post(url_send, json=payload, timeout=10.0)
            except Exception:
                pass

        def answer_callback(callback_id: str, text: str = "") -> None:
            try:
                _httpx.post(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                    json={"callback_query_id": callback_id, "text": text},
                    timeout=5.0,
                )
            except Exception:
                pass

        def _setup_bot() -> None:
            try:
                _httpx.post(
                    f"https://api.telegram.org/bot{token}/setMyCommands",
                    json={"commands": [
                        {"command": "start",  "description": "Главное меню"},
                        {"command": "status", "description": "Статус бота"},
                        {"command": "run",    "description": "Запустить бота"},
                        {"command": "stop",   "description": "Остановить бота"},
                        {"command": "pnl",    "description": "P&L за 7 дней"},
                        {"command": "opps",   "description": "Последние возможности"},
                    ]},
                    timeout=10.0,
                )
                _httpx.post(
                    f"https://api.telegram.org/bot{token}/setChatMenuButton",
                    json={"chat_id": chat_id, "menu_button": {
                        "type": "web_app",
                        "text": "📊 Дашборд",
                        "web_app": {"url": DASHBOARD_URL},
                    }},
                    timeout=10.0,
                )
            except Exception:
                pass

        _setup_bot()

        def _get_admin_uid() -> int:
            from web.auth import User
            admins = [u for u in User.all_users() if u.is_admin]
            return admins[0].id if admins else 0

        def _handle_status() -> None:
            uid = _get_admin_uid()
            env2 = read_env()
            dry = env2.get("DRY_RUN", "true").lower() == "true"
            running = bot_mgr.is_running(uid) if uid else False
            pnl = _read_today_pnl(uid)
            send_keyboard(
                f"*Статус ArbitrageBot*\n\n"
                f"Движок: {'🟢 Работает' if running else '🔴 Остановлен'}\n"
                f"Режим: {'🧪 DRY\\_RUN' if dry else '⚡ LIVE'}\n"
                f"Сегодня P&L: `${pnl:+.4f}`",
                MAIN_KEYBOARD,
            )

        def _handle_run() -> None:
            uid = _get_admin_uid()
            if not uid:
                send_keyboard("❌ Нет admin-пользователей.")
                return
            if bot_mgr.is_running(uid):
                send_keyboard("Бот уже запущен.", MAIN_KEYBOARD)
            else:
                user_env = get_effective_env(uid, _app_secret)
                ok, msg_txt = bot_mgr.start(uid, user_env)
                send_keyboard(f"🚀 {msg_txt}" if ok else f"❌ {msg_txt}", MAIN_KEYBOARD)

        def _handle_stop() -> None:
            uid = _get_admin_uid()
            if not uid:
                send_keyboard("❌ Нет admin-пользователей.")
                return
            if bot_mgr.is_running(uid):
                ok, msg_txt = bot_mgr.stop(uid)
                send_keyboard("🛑 Бот остановлен." if ok else f"❌ {msg_txt}", MAIN_KEYBOARD)
            else:
                send_keyboard("Бот уже остановлен.", MAIN_KEYBOARD)

        def _handle_opps() -> None:
            try:
                opps_data = get_opportunities(limit=5, min_pct=0.0)
                if not opps_data:
                    send_keyboard("Возможностей пока нет.", MAIN_KEYBOARD)
                else:
                    lines = ["*Последние арбитражные возможности:*\n"]
                    for o in opps_data[:5]:
                        pct   = o.get("profit_pct", 0)
                        title = (o.get("title") or "")[:35]
                        lines.append(f"• `{title}` — *+{pct:.2f}%*")
                    send_keyboard("\n".join(lines), MAIN_KEYBOARD)
            except Exception as e:
                send_keyboard(f"❌ Ошибка: {e}")

        def _handle_pnl() -> None:
            uid = _get_admin_uid()
            pnl_file = (bot_mgr.data_dir(uid) if uid else ROOT / "data") / "pnl_history.json"
            try:
                if pnl_file.is_file():
                    raw = json.loads(pnl_file.read_text(encoding="utf-8"))
                    items = sorted(raw.items())[-7:]
                    lines = ["*P&L за последние 7 дней:*\n"]
                    total = 0.0
                    for d, v in items:
                        total += v
                        lines.append(f"  {d}: `${v:+.4f}`")
                    lines.append(f"\n*Итого: `${total:+.4f}`*")
                    send_keyboard("\n".join(lines), MAIN_KEYBOARD)
                else:
                    send_keyboard("Данных P&L пока нет. Запусти бота чтобы начать.", MAIN_KEYBOARD)
            except Exception as e:
                send_keyboard(f"❌ Ошибка: {e}")

        with _httpx.Client(timeout=35.0) as client:
            while True:
                try:
                    resp = client.get(url_updates, params={
                        "offset": offset, "timeout": 30,
                        "allowed_updates": ["message", "callback_query"],
                    })
                    updates = resp.json().get("result", [])
                except Exception:
                    time.sleep(5)
                    continue

                for upd in updates:
                    offset = upd["update_id"] + 1

                    # ── Inline button callbacks ──────────────────────────
                    if "callback_query" in upd:
                        cb   = upd["callback_query"]
                        cb_id   = cb["id"]
                        cb_data = cb.get("data", "")
                        cb_from = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
                        if cb_from != chat_id:
                            answer_callback(cb_id)
                            continue
                        answer_callback(cb_id, "⏳")
                        if cb_data == "status": _handle_status()
                        elif cb_data == "run":  _handle_run()
                        elif cb_data == "stop": _handle_stop()
                        elif cb_data == "opps": _handle_opps()
                        elif cb_data == "pnl":  _handle_pnl()
                        continue

                    # ── Text commands ────────────────────────────────────
                    msg     = upd.get("message", {})
                    from_id = str((msg.get("chat") or {}).get("id", ""))
                    if from_id != chat_id:
                        continue

                    text = (msg.get("text") or "").strip()
                    cmd  = text.split("@")[0].lower()

                    if cmd in ("/start", "/help", "/menu"):
                        send_keyboard(
                            "⚡ *ArbitrageBot*\n\nВыбери действие:",
                            MAIN_KEYBOARD,
                        )
                    elif cmd == "/status": _handle_status()
                    elif cmd == "/run":    _handle_run()
                    elif cmd == "/stop":   _handle_stop()
                    elif cmd == "/opps":   _handle_opps()
                    elif cmd == "/pnl":    _handle_pnl()
                    elif cmd.startswith("/"):
                        send_keyboard(f"Неизвестная команда: `{cmd}`", MAIN_KEYBOARD)

    # Re-read env each restart in case token changed
    def _supervisor():
        while True:
            env = read_env()
            token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
            if token:
                try:
                    _poll()
                except Exception:
                    pass
            time.sleep(30)  # retry after 30s if poll exits

    t = threading.Thread(target=_supervisor, name="TelegramPoll", daemon=True)
    t.start()
