"use strict";
const $ = (id) => document.getElementById(id);
const demo = new URLSearchParams(location.search).get("demo") === "1";
const sample = {
  id: "example",
  name: "Анна Кузнецова",
  category: "Гость",
  points: 0,
  checked_in: false,
  history: [],
};
let currentGuest = null,
  authorized = false,
  stream = null,
  timer = null,
  scanning = false;
let sessionToken = null;
let pointsPerCheckin = 10;
const api = async (path, body) => {
  const response = await fetch(`api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Guestpass-Request": "1",
      ...(sessionToken ? { Authorization: `Bearer ${sessionToken}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok)
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : "Не удалось выполнить запрос",
    );
  return data;
};
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = false;
}
function initials(name) {
  return name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((x) => Array.from(x)[0])
    .join("")
    .toUpperCase();
}
function stats(values) {
  $("registered").textContent = values.registered;
  $("checked-in").textContent = values.checked_in;
  $("total-points").textContent = values.points;
  const fraction = values.registered
    ? Math.min(100, Math.round((100 * values.checked_in) / values.registered))
    : 0;
  $("percentage").textContent = `${fraction}%`;
  $("progress").style.width = `${fraction}%`;
}
function showGuest(guest) {
  currentGuest = guest;
  if (guest.event_name) $("event-name").textContent = guest.event_name;
  if (typeof guest.review_mode === "boolean") {
    $("mode-label").textContent = guest.review_mode
      ? "Общее демомероприятие"
      : "Основное мероприятие";
  }
  $("guest-name").textContent = guest.name;
  $("guest-avatar").textContent = initials(guest.name);
  $("guest-category").textContent = guest.category;
  $("guest-serial").textContent = demo
    ? "#000128"
    : "#" + guest.id.replaceAll("-", "").slice(0, 6).toUpperCase();
  $("guest-points").textContent = guest.points;
  $("guest-arrival").textContent = guest.checked_in ? "Отмечен" : "Не отмечен";
  $("guest-status").textContent = guest.checked_in
    ? "Уже на мероприятии"
    : "Ожидает регистрации";
  $("guest-status").classList.toggle("done", guest.checked_in);
  $("check-in-button").disabled = guest.checked_in || (!authorized && !demo);
  $("check-in-button").textContent = guest.checked_in
    ? "Приход уже отмечен"
    : `Отметить приход · +${pointsPerCheckin}`;
  $("history").hidden = !guest.history.length;
  $("history-list").replaceChildren();
  for (const entry of guest.history) {
    const li = document.createElement("li"),
      strong = document.createElement("strong");
    strong.textContent = `+${entry.amount} · ${entry.reason}`;
    const time = new Date(entry.created_at).toLocaleString("ru-RU", {
      timeZone: "Europe/Moscow",
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    li.append(
      strong,
      document.createElement("br"),
      document.createTextNode(
        `${time} МСК · организатор ${entry.actor_telegram_id}`,
      ),
    );
    $("history-list").append(li);
  }
}
function revealScannedGuest(guest) {
  showGuest(guest);
  if (!demo)
    api("stats")
      .then(stats)
      .catch(() => {});
  if (!window.matchMedia("(max-width: 750px)").matches) return;
  // Wait for the updated card and stopped video to take their final height.
  requestAnimationFrame(() => {
    $("guest-name").focus({ preventScroll: true });
    $("guest-card").scrollIntoView({
      block: "start",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
    });
  });
}
function stopCamera() {
  if (timer) clearTimeout(timer);
  timer = null;
  stream?.getTracks().forEach((t) => t.stop());
  stream = null;
  $("camera").srcObject = null;
  $("camera").hidden = true;
  $("camera-placeholder").hidden = false;
  $("camera-button").textContent = "Включить камеру";
}
function mode(name) {
  stopCamera();
  for (const button of document.querySelectorAll("[data-mode]")) {
    const active = button.dataset.mode === name;
    button.setAttribute("aria-selected", String(active));
    $(`pane-${button.dataset.mode}`).hidden = !active;
  }
}
for (const button of document.querySelectorAll("[data-mode]")) {
  button.addEventListener("click", () => mode(button.dataset.mode));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = [...document.querySelectorAll("[data-mode]")];
    let index = buttons.indexOf(button);
    index =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? 2
          : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
    buttons[index].click();
    buttons[index].focus();
  });
}
function access() {
  if (demo) {
    notice(
      "Учебный пример: реальные гости и баллы не меняются. Для регистрации откройте сканер через Telegram-бота.",
    );
    return false;
  }
  if (!authorized) {
    notice(
      "Откройте Telegram-бота и выберите /organizer. Сканер доступен организаторам.",
      true,
    );
    return false;
  }
  return true;
}
async function scanToken(token) {
  const guest = await api("scan", { token: token.trim() });
  $("notice").hidden = true;
  stopCamera();
  revealScannedGuest(guest);
}
$("code-button").addEventListener("click", async () => {
  if (!access()) return;
  const token = $("token-input").value.trim();
  if (!token) {
    notice("Вставьте персональный код гостя.", true);
    return;
  }
  const button = $("code-button");
  button.disabled = true;
  try {
    await scanToken(token);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("token-input").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter")
    $("code-button").click();
});
$("photo-input").addEventListener("change", () => {
  const file = $("photo-input").files[0];
  $("photo-name").textContent =
    file?.name || "Код должен целиком помещаться в кадре.";
  $("photo-button").disabled = !file;
});
function imageBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(Error("Не удалось открыть файл"));
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.readAsDataURL(file);
  });
}
$("photo-button").addEventListener("click", async () => {
  if (!access()) return;
  const file = $("photo-input").files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    notice("Выберите картинку до 10 МБ.", true);
    return;
  }
  const button = $("photo-button");
  button.disabled = true;
  button.textContent = "Распознаём…";
  try {
    const guest = await api("scan-photo", {
      image_base64: await imageBase64(file),
    });
    $("notice").hidden = true;
    revealScannedGuest(guest);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Распознать пропуск";
  }
});
$("camera-button").addEventListener("click", async () => {
  if (stream) {
    stopCamera();
    return;
  }
  if (!access()) return;
  if (!navigator.mediaDevices?.getUserMedia) {
    notice(
      "Камера недоступна. Откройте сайт по HTTPS или загрузите фото QR.",
      true,
    );
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } },
      audio: false,
    });
    $("camera").srcObject = stream;
    $("camera").hidden = false;
    $("camera-placeholder").hidden = true;
    await $("camera").play();
    $("camera-button").textContent = "Остановить камеру";
    const detector =
      "BarcodeDetector" in window
        ? new BarcodeDetector({ formats: ["qr_code"] })
        : null;
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    const tick = async () => {
      if (!stream || document.hidden) return;
      if (scanning) {
        timer = setTimeout(tick, 800);
        return;
      }
      scanning = true;
      try {
        if (detector) {
          const found = await detector.detect($("camera"));
          if (found.length === 1) await scanToken(found[0].rawValue);
          else if (found.length > 1)
            notice("Покажите один QR-код крупнее.", true);
        } else {
          const video = $("camera");
          if (video.videoWidth) {
            canvas.width = 900;
            canvas.height = Math.round(
              (900 * video.videoHeight) / video.videoWidth,
            );
            context.drawImage(video, 0, 0, canvas.width, canvas.height);
            const guest = await api("scan-photo", {
              image_base64: canvas.toDataURL("image/jpeg", 0.85).split(",")[1],
            });
            stopCamera();
            revealScannedGuest(guest);
          }
        }
      } catch (error) {
        if (!error.message.includes("QR не распознан"))
          notice(error.message, true);
      } finally {
        scanning = false;
        if (stream) timer = setTimeout(tick, 1200);
      }
    };
    timer = setTimeout(tick, 500);
  } catch (error) {
    stopCamera();
    notice(
      error.name === "NotAllowedError"
        ? "Разрешите доступ к камере или используйте фото QR."
        : "Не удалось включить камеру. Используйте загрузку фото.",
      true,
    );
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopCamera();
});
window.addEventListener("pagehide", stopCamera);
function refreshCounters() {
  if (authorized && !demo && !document.hidden)
    api("stats")
      .then(stats)
      .catch(() => {});
}
setInterval(refreshCounters, 5000);
window.addEventListener("focus", refreshCounters);
$("check-in-button").addEventListener("click", async () => {
  if (!currentGuest) return;
  const button = $("check-in-button");
  button.disabled = true;
  if (demo) {
    sample.points = 10;
    sample.checked_in = true;
    sample.history = [
      {
        amount: 10,
        reason: "Приход на мероприятие",
        actor_telegram_id: "пример",
        created_at: new Date().toISOString(),
      },
    ];
    showGuest(sample);
    stats({ registered: 128, checked_in: 65, points: 650 });
    notice("Пример: приход отмечен. Повторное начисление недоступно.");
    return;
  }
  try {
    const result = await api(
      `guests/${encodeURIComponent(currentGuest.id)}/check-in`,
      {},
    );
    showGuest(result.guest);
    stats(await api("stats"));
    notice(
      result.awarded
        ? "Приход отмечен. Баллы добавлены в историю."
        : "Этот приход уже был отмечен. Баллы не начислены повторно.",
    );
  } catch (error) {
    button.disabled = false;
    notice(error.message, true);
  }
});
$("logout").addEventListener("click", async () => {
  try {
    await api("logout", {});
    sessionToken = null;
    if (window.Telegram?.WebApp?.initData) window.Telegram.WebApp.close();
    else location.reload();
  } catch (error) {
    notice(error.message, true);
  }
});
async function init() {
  if (!demo)
    try {
      const meta = await api("meta");
      $("event-name").textContent = meta.event_name;
      pointsPerCheckin = meta.check_in_points;
      $("check-in-button").textContent =
        `Отметить приход · +${pointsPerCheckin}`;
      if (meta.bot_username)
        $("bot-link").href =
          `https://t.me/${encodeURIComponent(meta.bot_username)}`;
      else
        $("bot-link").addEventListener("click", (event) => {
          event.preventDefault();
          notice("Бот ещё не настроен владельцем сервиса.", true);
        });
    } catch {
      notice(
        "Не удалось подключиться к сервису. Попробуйте обновить страницу.",
        true,
      );
    }
  if (demo) {
    document.body.classList.add("demo");
    $("mode-label").textContent = "Демо · вымышленные данные";
    $("access-note").hidden = true;
    $("account-name").textContent = "Роман";
    $("account-role").textContent = "Организатор · пример";
    $("account-avatar").textContent = "Р";
    stats({ registered: 128, checked_in: 64, points: 640 });
    showGuest(sample);
    return;
  }
  const telegram = window.Telegram?.WebApp;
  try {
    if (telegram?.initData) {
      telegram.ready();
      telegram.expand();
      try {
        sessionToken = (await api("session", { init_data: telegram.initData }))
          .session_token;
      } catch {
        // A still-valid HttpOnly session can survive a WebView reload with older initData.
      }
    }
    const me = await api("me");
    authorized = true;
    $("account-name").textContent = "Организатор";
    $("account-avatar").textContent = "О";
    $("account-role").textContent = `ID ${me.organizer_id}`;
    $("access-note").hidden = true;
    $("logout").hidden = false;
    $("event-name").textContent = me.event_name;
    if (me.review_mode) {
      $("mode-label").textContent = "Общее демомероприятие";
      notice(
        "Общий демостенд: здесь видны регистрации всех участников. /start в боте выдаёт каждому персональный QR.",
      );
    }
    stats(await api("stats"));
  } catch {
    authorized = false;
  }
}
init();
