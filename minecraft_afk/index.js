/**
 * Aternos AFK-бот
 * ================
 *
 * Подключается к Minecraft-серверу как обычный игрок и остаётся там,
 * периодически слегка двигаясь, чтобы сервер не счёл его неактивным
 * (некоторые сервера/плагины кикают за долгую неподвижность) и не
 * заснул из-за отсутствия игроков (актуально для Aternos — сервер
 * автоматически выключается, когда все выходят).
 *
 * ⚠️ ВАЖНО — ПРОЧТИТЕ ПЕРЕД ЗАПУСКОМ
 * -----------------------------------
 * Aternos прямо запрещает это в своих условиях использования и
 * официально пишет, что отслеживает "искусственную активность":
 * серверы, удерживаемые в сети ботами, могут быть отключены, а
 * аккаунт — ограничен или заблокирован. Это не гарантированно
 * произойдёт, но риск реальный и это ваше осознанное решение,
 * не разработчика скрипта. Сделайте бэкап мира на всякий случай.
 *
 * НАСТРОЙКА
 * ---------
 * Переменные окружения (задаются в .env локально или в Environment
 * на Render):
 *   MC_HOST      — адрес сервера, например "Aigakeru.aternos.me"
 *   MC_PORT      — порт, например "26538"
 *   MC_USERNAME  — ник бота в игре (для offline-режима — любой,
 *                  главное чтобы не совпадал с реальным игроком)
 *   MC_VERSION   — версия игры, например "1.20.4" (можно оставить
 *                  пустым — mineflayer попробует определить сама,
 *                  но с явной версией стабильнее)
 *   MC_AUTH      — "offline" (по умолчанию) для большинства серверов
 *                  без лицензии/microsoft-аккаунта у бота
 */

const mineflayer = require('mineflayer');
const http = require('http');

let autoEatPlugin = null;
try {
  // Плагин необязательный: если бот заходит на сервер без еды в
  // инвентаре или без прав на её получение — просто не используется.
  autoEatPlugin = require('mineflayer-auto-eat').plugin;
} catch (e) {
  // пакет не установлен — работаем без автоеды
}

const MC_HOST = process.env.MC_HOST;
const MC_PORT = parseInt(process.env.MC_PORT || '25565', 10);
const MC_USERNAME = process.env.MC_USERNAME || 'AFKBot';
const MC_VERSION = process.env.MC_VERSION || false; // false = автоопределение
const MC_AUTH = process.env.MC_AUTH || 'offline';

if (!MC_HOST) {
  console.error('Не задан MC_HOST — укажите адрес сервера в переменных окружения.');
  process.exit(1);
}

let bot = null;
let reconnectTimer = null;
let antiAfkTimer = null;

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

function scheduleReconnect(delayMs) {
  if (reconnectTimer) return;
  log(`Переподключение через ${Math.round(delayMs / 1000)} сек...`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    createBot();
  }, delayMs);
}

function startAntiAfk() {
  stopAntiAfk();
  antiAfkTimer = setInterval(() => {
    if (!bot || !bot.entity) return;
    try {
      // Небольшой прыжок + случайный поворот взгляда — этого обычно
      // достаточно, чтобы сервер не считал игрока полностью неактивным.
      bot.setControlState('jump', true);
      setTimeout(() => bot && bot.setControlState('jump', false), 300);

      const yaw = Math.random() * Math.PI * 2 - Math.PI;
      const pitch = (Math.random() * 0.6 - 0.3);
      bot.look(yaw, pitch, true);
    } catch (e) {
      log('Ошибка anti-AFK действия:', e.message);
    }
  }, 25000); // каждые ~25 секунд
}

function stopAntiAfk() {
  if (antiAfkTimer) {
    clearInterval(antiAfkTimer);
    antiAfkTimer = null;
  }
}

function createBot() {
  log(`Подключаюсь к ${MC_HOST}:${MC_PORT} как ${MC_USERNAME}...`);

  bot = mineflayer.createBot({
    host: MC_HOST,
    port: MC_PORT,
    username: MC_USERNAME,
    version: MC_VERSION,
    auth: MC_AUTH,
  });

  if (autoEatPlugin) {
    bot.loadPlugin(autoEatPlugin);
  }

  bot.once('spawn', () => {
    if (bot.autoEat) {
      // Автоматически ест, когда голод опускается ниже порога, если
      // в инвентаре есть подходящая еда.
      bot.autoEat.options = { priority: 'foodPoints', startAt: 14, bannedFood: [] };
    }
  });

  bot.on('spawn', () => {
    log('Заспавнился на сервере, запускаю anti-AFK.');
    startAntiAfk();
  });

  bot.on('health', () => {
    if (bot.health <= 0) {
      log(`Здоровье на нуле (${bot.health}) — жду события death для респавна.`);
    } else if (bot.health <= 6) {
      log(`Низкое здоровье: ${bot.health}/20, голод: ${bot.food}/20.`);
    }
  });

  bot.on('death', () => {
    // Бот умер (от моба, лавы, голода, падения и т.п.) — respawn()
    // отправляет пакет респавна и возвращает персонажа на точку спавна.
    // Без этого бот так и останется висеть на экране смерти.
    log('Персонаж умер — респавню.');
    try {
      bot.respawn();
    } catch (e) {
      log('Не удалось выполнить respawn():', e.message);
    }
  });

  bot.on('kicked', (reason) => {
    log('Бота кикнули:', reason);
  });

  bot.on('error', (err) => {
    log('Ошибка соединения:', err.message);
  });

  bot.on('end', (reason) => {
    log('Соединение закрыто:', reason);
    stopAntiAfk();
    bot = null;
    scheduleReconnect(15000);
  });
}

createBot();

// Минимальный HTTP-сервер — нужен только для хостингов вроде Render,
// которым обязательно открыть порт, и чтобы UptimeRobot мог пинговать
// сервис и не давать бесплатному тарифу засыпать.
// Health-check сервер нужен, только если этот скрипт запущен САМ ПО
// СЕБЕ как отдельный Render-сервис. Если он запущен как подпроцесс
// внутри Python-моста (объединённый деплой) — Python сам не передаёт
// сюда переменную PORT (порт держит только он), и этот блок просто
// не активируется.
const port = process.env.PORT;
if (port) {
  http
    .createServer((req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end(`OK — бот ${bot ? 'подключён' : 'переподключается'} к ${MC_HOST}:${MC_PORT}`);
    })
    .listen(port, () => log(`HTTP health-check слушает порт ${port}`));
}
