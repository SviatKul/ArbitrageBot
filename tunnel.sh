#!/usr/bin/env bash
# Публичный HTTPS доступ к дашборду.
# Пробует cloudflared, при ошибке DNS — запускает localtunnel.
# Использование: ./tunnel.sh [PORT]

PORT="${1:-8080}"
LOG="/Users/mac/Desktop/ArbitrageBot/logs/tunnel.log"

mkdir -p "$(dirname "$LOG")"

# Проверить что дашборд запущен
if ! curl -s "http://localhost:${PORT}/health" --max-time 3 | grep -q '"ok":true'; then
    echo "Дашборд не отвечает на http://localhost:${PORT}/health"
    echo "Запустите сначала: python web.py"
    exit 1
fi

# ── Попытка 1: cloudflared ──────────────────────────────────────────────────
if command -v cloudflared &>/dev/null; then
    echo "Запускаю Cloudflare Tunnel → http://localhost:${PORT}"
    echo ""

    # Запускаем и ловим URL в течение 20 секунд
    TMPLOG=$(mktemp)
    cloudflared tunnel --url "http://localhost:${PORT}" --no-autoupdate 2>&1 > "$TMPLOG" &
    CF_PID=$!

    for i in $(seq 1 20); do
        sleep 1
        URL=$(grep -oE 'https://[^ ]+trycloudflare\.com' "$TMPLOG" 2>/dev/null | head -1)
        if [ -n "$URL" ]; then
            cat "$TMPLOG" >> "$LOG"
            echo "=================================="
            echo "  Публичный URL: $URL"
            echo "=================================="
            echo ""
            echo "Подели этим URL с другими. Нажми Ctrl+C чтобы остановить."
            # Не выходим — ждём завершения процесса
            wait $CF_PID
            rm -f "$TMPLOG"
            exit 0
        fi
        # Проверим что процесс ещё жив
        if ! kill -0 $CF_PID 2>/dev/null; then
            break
        fi
    done

    # cloudflared умер или не дал URL — пробуем fallback
    kill $CF_PID 2>/dev/null
    cat "$TMPLOG" >> "$LOG"
    rm -f "$TMPLOG"
    echo "cloudflared не смог установить туннель (ошибка DNS)."
    echo ""
    echo "Чтобы починить cloudflared навсегда:"
    echo "  В этом же чате: ! sudo brew services start dnsmasq"
    echo ""
fi

# ── Попытка 2: localtunnel (через npx) ─────────────────────────────────────
if command -v npx &>/dev/null; then
    echo "Запускаю localtunnel → http://localhost:${PORT}"
    echo ""

    TMPLOG=$(mktemp)
    npx localtunnel --port "$PORT" 2>&1 > "$TMPLOG" &
    LT_PID=$!

    for i in $(seq 1 30); do
        sleep 1
        URL=$(grep -oE 'https://[^ ]+\.loca\.lt' "$TMPLOG" 2>/dev/null | head -1)
        if [ -n "$URL" ]; then
            cat "$TMPLOG" >> "$LOG"
            echo "=================================="
            echo "  Публичный URL: $URL"
            echo "=================================="
            echo ""
            echo "Внимание: при первом открытии страницы localtunnel покажет"
            echo "предупреждение — нажми 'Click to Continue'."
            echo ""
            echo "Подели этим URL с другими. Нажми Ctrl+C чтобы остановить."
            wait $LT_PID
            rm -f "$TMPLOG"
            exit 0
        fi
        if ! kill -0 $LT_PID 2>/dev/null; then
            break
        fi
    done

    kill $LT_PID 2>/dev/null
    cat "$TMPLOG" >> "$LOG"
    rm -f "$TMPLOG"
    echo "localtunnel тоже не запустился. Проверь интернет-соединение."
    exit 1
fi

echo "Не найдено ни cloudflared, ни npx."
echo "Установи: brew install cloudflared  или  brew install node"
exit 1
