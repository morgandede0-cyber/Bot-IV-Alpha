import asyncio
import datetime
import io
import os
import random
import time
import sqlite3
import traceback
import json
import aiohttp
import discord
from discord import app_commands, ui
from PIL import Image, ImageDraw, ImageFont, ImageOps
from discord.ext import commands

# ==========================================
# 1. CONFIGURATION INITIALE & CONSTANTES
# ==========================================

TOKEN = (
    os.getenv("TAVERNE_TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
)
MAX_BET = 500

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

cooldowns = {}
TEST_MODE_ENABLED = False

# ==========================================
# REDIS CONNECTION (Optionnel)
# ==========================================

try:
    import redis
    REDIS_INSTALLED = True
except ImportError:
    REDIS_INSTALLED = False
    print("⚠️ Redis non installé, utilisation du cache mémoire")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_AVAILABLE = False

if REDIS_INSTALLED:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        REDIS_AVAILABLE = True
        print("✅ Redis connecté avec succès !")
    except Exception as e:
        print(f"⚠️ Redis non disponible: {e}, utilisation du cache mémoire")
        redis_client = None
else:
    redis_client = None


# ==========================================
# SALON DE LOGS PUBLIC (SALON B)
# ==========================================

PUBLIC_LOG_CHANNEL_ID = 1540068629389910087  # Salon "taverne"

async def send_public_log(content: str = None, embed: discord.Embed = None, file: discord.File = None, view: ui.View = None):
    """Envoie un message public dans le Salon B sans séparateurs."""
    if PUBLIC_LOG_CHANNEL_ID:
        channel = bot.get_channel(PUBLIC_LOG_CHANNEL_ID)
        if channel:
            try:
                return await channel.send(content=content, embed=embed, file=file, view=view)
            except Exception as e:
                print(f"❌ Erreur envoi log public : {e}")
    return None


# ==========================================
# 2. GESTIONNAIRE D'ANIMATION DE MESSAGE
# ==========================================

class AnimatedMessageManager:
    def __init__(self, interaction: discord.Interaction, show_animation: bool = True):
        self.interaction = interaction
        self.show_animation = show_animation
        self.last_content = None
        self.last_embed = None
        self.first_update = True

    async def update_animation(self, new_content: str = None, new_embed: discord.Embed = None, view: ui.View = None):
        if not self.show_animation:
            return

        if new_content != self.last_content or new_embed != self.last_embed:
            try:
                if self.first_update:
                    await self.interaction.edit_original_response(content=new_content, embed=new_embed, view=view)
                    self.first_update = False
                else:
                    await self.interaction.edit_original_response(content=new_content, embed=new_embed, view=view)
                self.last_content = new_content
                self.last_embed = new_embed
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(1)


# ==========================================
# 3. GESTION DE LA BASE DE DONNÉES (SQLite Local)
# ==========================================

def get_db_connection():
    conn = sqlite3.connect("/data/economy.db")
    return conn


def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                wallet INTEGER DEFAULT 0,
                bank INTEGER DEFAULT 0,
                last_daily INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0,
                beers_today INTEGER DEFAULT 0,
                last_beer_date TEXT DEFAULT '',
                games_played INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0,
                games_lost INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_channels (
                guild_id INTEGER,
                ai_type TEXT,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, ai_type)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                show_animations INTEGER DEFAULT 1
            )
        """)
        
        # ========== TABLES POUR LES QUÊTES ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS public_quests (
                quest_date TEXT PRIMARY KEY,
                quests_json TEXT,
                generated_at INTEGER
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS player_quests (
                user_id INTEGER,
                quest_date TEXT,
                quest_key TEXT,
                progress INTEGER DEFAULT 0,
                completed INTEGER DEFAULT 0,
                claimed INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, quest_date, quest_key)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quest_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                message_id INTEGER
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_quests (
                user_id INTEGER,
                quest_date TEXT,
                quest_key TEXT,
                description TEXT,
                target INTEGER,
                progress INTEGER DEFAULT 0,
                reward INTEGER,
                claimed INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, quest_date, quest_key)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                user_id INTEGER,
                achievement_key TEXT,
                tier INTEGER DEFAULT 1,
                unlocked_at TEXT,
                PRIMARY KEY (user_id, achievement_key)
            )
        """)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='story_progress'")
        story_table_exists = cursor.fetchone() is not None

        if not story_table_exists:
            cursor.execute("""
                CREATE TABLE story_progress (
                    user_id INTEGER,
                    episode_id INTEGER,
                    unlocked_at TEXT,
                    PRIMARY KEY (user_id, episode_id)
                )
            """)
        else:
            cursor.execute("PRAGMA table_info(story_progress)")
            story_columns = [column[1] for column in cursor.fetchall()]

            if "episode_id" not in story_columns and "episode" in story_columns:
                cursor.execute("ALTER TABLE story_progress ADD COLUMN episode_id INTEGER")
                cursor.execute("UPDATE story_progress SET episode_id = episode WHERE episode_id IS NULL")

            if "unlocked_at" not in story_columns:
                cursor.execute("ALTER TABLE story_progress ADD COLUMN unlocked_at TEXT")
                cursor.execute(
                    "UPDATE story_progress SET unlocked_at = ? WHERE unlocked_at IS NULL OR unlocked_at = ''",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),)
                )

            cursor.execute("PRAGMA table_info(story_progress)")
            story_columns = [column[1] for column in cursor.fetchall()]
            if "episode" in story_columns and "episode_id" in story_columns:
                cursor.execute("UPDATE story_progress SET episode_id = episode WHERE episode_id IS NULL")

            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_story_progress_user_episode
                ON story_progress(user_id, episode_id)
            """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER,
                item_name TEXT,
                quantity INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, item_name)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                item_key TEXT PRIMARY KEY,
                name TEXT,
                price INTEGER,
                description TEXT,
                shop_type TEXT DEFAULT 'normal',
                episode INTEGER DEFAULT 0,
                required_role_id INTEGER DEFAULT NULL,
                role_to_give_id INTEGER DEFAULT NULL
            )
        """)

        cursor.execute("PRAGMA table_info(shop_items)")
        shop_columns = [column[1] for column in cursor.fetchall()]
        for col, col_type in [
            ("shop_type", "TEXT DEFAULT 'normal'"),
            ("episode", "INTEGER DEFAULT 0"),
            ("required_role_id", "INTEGER DEFAULT NULL"),
            ("role_to_give_id", "INTEGER DEFAULT NULL"),
        ]:
            if col not in shop_columns:
                cursor.execute(f"ALTER TABLE shop_items ADD COLUMN {col} {col_type}")

        cursor.execute("SELECT COUNT(*) FROM shop_items")
        if cursor.fetchone()[0] == 0:
            cursor.executemany(
                "INSERT INTO shop_items (item_key, name, price, description, shop_type, episode, required_role_id, role_to_give_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("A1", "👑 Rôle VIP", 5000, "Un statut de VIP sur le serveur.", "normal", 0, None, None),
                    ("A2", "🎁 Boîte Mystère", 1000, "Contient une surprise aléatoire !", "normal", 0, None, None),
                    ("SP1", "💎 Épée Légendaire", 25000, "Une arme surpuissante réservée aux VIP.", "special", 0, None, None),
                ],
            )

        episode_items = []
        for ep in range(1, 26):
            label = f"0{ep:02d}" if ep < 10 else f"{ep}"
            for suffix, name in [("1", "Alpha"), ("2", "Bêta"), ("3", "Gamma"), ("4", "Delta")]:
                episode_items.append((
                    f"EP{ep}_{suffix}",
                    f"Relique {name} [{label}]",
                    500,
                    "Objet d'histoire essentiel.",
                    "episode", ep, None, None,
                ))
        cursor.executemany(
            "INSERT OR IGNORE INTO shop_items (item_key, name, price, description, shop_type, episode, required_role_id, role_to_give_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            episode_items,
        )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_last_chapter (
                user_id INTEGER PRIMARY KEY,
                last_episode INTEGER DEFAULT 1,
                last_shop_episode INTEGER DEFAULT 1
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quest_reward_state (
                user_id INTEGER PRIMARY KEY,
                base_reward INTEGER DEFAULT 0,
                quest_date TEXT DEFAULT '',
                quest_streak INTEGER DEFAULT 0,
                last_claim_date TEXT DEFAULT ''
            )
        """)

        columns_to_add = [
            ("last_daily", "INTEGER DEFAULT 0"),
            ("streak", "INTEGER DEFAULT 0"),
            ("beers_today", "INTEGER DEFAULT 0"),
            ("last_beer_date", "TEXT DEFAULT ''"),
            ("games_played", "INTEGER DEFAULT 0"),
            ("games_won", "INTEGER DEFAULT 0"),
            ("games_lost", "INTEGER DEFAULT 0"),
        ]
        for col, col_type in columns_to_add:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
            except Exception:
                pass
        conn.commit()


# ==========================================
# 3.1. FONCTIONS UTILITAIRES
# ==========================================

def get_user(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT wallet, bank, last_daily, streak, beers_today, last_beer_date, games_played, games_won, games_lost FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO users (user_id, wallet, bank, last_daily, streak, beers_today, last_beer_date, games_played, games_won, games_lost)"
                " VALUES (?, 100, 0, 0, 0, 0, '', 0, 0, 0)",
                (user_id,),
            )
            conn.commit()
            return 100, 0, 0, 0, 0, '', 0, 0, 0
        return (
            (row[0] or 0),
            (row[1] or 0),
            (row[2] or 0),
            (row[3] or 0),
            (row[4] or 0),
            (row[5] or ''),
            (row[6] or 0),
            (row[7] or 0),
            (row[8] or 0),
        )


def update_wallet(user_id: int, amount: int):
    get_user(user_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET wallet = COALESCE(wallet, 0) + ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.commit()
    
    if REDIS_AVAILABLE and redis_client:
        redis_client.delete(f"user:{user_id}")
        wallet, bank, _, _, _, _, _, _, _ = get_user(user_id)
        redis_client.zadd("leaderboard:richest", {str(user_id): wallet + bank})
    
    if amount > 0:
        update_quest_progress(user_id, "money_earned", amount)
        update_quest_progress_v2(user_id, "money_earned", amount)
    
    asyncio.create_task(check_and_unlock_achievements(user_id, bot))


def update_game_stats(user_id: int, won: bool):
    get_user(user_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if won:
            cursor.execute("UPDATE users SET games_played = COALESCE(games_played, 0) + 1, games_won = COALESCE(games_won, 0) + 1 WHERE user_id = ?", (user_id,))
        else:
            cursor.execute("UPDATE users SET games_played = COALESCE(games_played, 0) + 1, games_lost = COALESCE(games_lost, 0) + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    
    if REDIS_AVAILABLE and redis_client:
        redis_client.delete(f"user:{user_id}")
    
    update_quest_progress(user_id, "games_played", 1)
    update_quest_progress_v2(user_id, "games_played", 1)
    if won:
        update_quest_progress(user_id, "games_won", 1)
        update_quest_progress_v2(user_id, "games_won", 1)
    
    asyncio.create_task(check_and_unlock_achievements(user_id, bot))


def format_currency(amount: int) -> str:
    return f"{amount:,} $".replace(",", " ")


def check_cooldown(user_id: int, command_name: str, duration: int) -> int:
    if TEST_MODE_ENABLED:
        return 0
    
    if REDIS_AVAILABLE and redis_client:
        key = f"cooldown:{user_id}:{command_name}"
        ttl = redis_client.ttl(key)
        if ttl > 0:
            return ttl
        redis_client.setex(key, duration, "1")
        return 0
    
    now = int(time.time())
    key = (user_id, command_name)
    expire = cooldowns.get(key, 0)
    if now < expire:
        return expire - now
    cooldowns[key] = now + duration
    return 0


def clear_cooldown(user_id: int, command_name: str = None):
    if REDIS_AVAILABLE and redis_client:
        if command_name:
            redis_client.delete(f"cooldown:{user_id}:{command_name}")
        else:
            keys = redis_client.keys(f"cooldown:{user_id}:*")
            if keys:
                redis_client.delete(*keys)
    else:
        if command_name:
            cooldowns.pop((user_id, command_name), None)
        else:
            keys_to_remove = [k for k in cooldowns if k[0] == user_id]
            for k in keys_to_remove:
                cooldowns.pop(k, None)


async def validate_game_bet(
    interaction: discord.Interaction, command_name: str, bet: int, cooldown_sec: int = 3600
) -> bool:
    async def reject(message: str):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    if bet <= 0:
        await reject("❌ La mise doit être supérieure à 0 $ !")
        return False
    if bet > MAX_BET:
        await reject(f"❌ La mise maximale autorisée est de **{MAX_BET} $** !")
        return False
    wallet = get_user(interaction.user.id)[0]
    if wallet < bet:
        await reject("❌ Solde insuffisant dans ton portefeuille ! Pense à retirer de l'argent via /banque.")
        return False
    retry_after = check_cooldown(interaction.user.id, command_name, cooldown_sec)
    if retry_after > 0:
        minutes, seconds = divmod(retry_after, 60)
        await reject(f"⏳ Tu dois attendre **{minutes} min et {seconds} sec** avant de pouvoir rejouer.")
        return False
    return True


init_db()


# ==========================================
# 4. SYSTÈME DE QUÊTES PUBLIC
# ==========================================

QUEST_POOL = [
    {"key": "games_played", "label": "🎲 Joueur Assidu", "desc": "Jouer {target} partie(s) dans un jeu de casino", "target_range": (5, 10)},
    {"key": "games_won", "label": "🏆 Chanceux du Jour", "desc": "Gagner {target} partie(s) dans n'importe quel jeu", "target_range": (3, 6)},
    {"key": "work_done", "label": "💼 Travailleur", "desc": "Travailler {target} fois via /work", "target_range": (2, 4)},
    {"key": "arena_fight", "label": "⚔️ Guerrier de l'Arène", "desc": "Affronter Bob dans l'arène {target} fois", "target_range": (2, 4)},
    {"key": "beer_drunk", "label": "🍺 Bon Vivant", "desc": "Commander {target} pinte(s) chez Jim", "target_range": (2, 4)},
    {"key": "pmu_bet", "label": "🐎 Turfiste", "desc": "Parier sur une course chez Brook {target} fois", "target_range": (2, 4)},
    {"key": "vault_attempt", "label": "🔐 Braqueur de Coffre", "desc": "Tenter de braquer la Brinks {target} fois", "target_range": (1, 3)},
    {"key": "crime_attempt", "label": "🥷 Petite Frappe", "desc": "Tenter un crime chez John {target} fois", "target_range": (2, 4)},
    {"key": "money_earned", "label": "💰 Homme d'Affaires", "desc": "Gagner un total de {target} $", "target_range": (1000, 3000)},
    {"key": "bank_deposit", "label": "🏦 Épargnant", "desc": "Déposer à la banque {target} fois", "target_range": (2, 4)},
    {"key": "pay_sent", "label": "💸 Généreux", "desc": "Envoyer de l'argent via /pay {target} fois", "target_range": (1, 3)},
    {"key": "blackjack_win", "label": "👑 Roi du Blackjack", "desc": "Gagner {target} partie(s) de Blackjack", "target_range": (1, 3)},
    {"key": "slots_win", "label": "🪙 Maître des Slots", "desc": "Gagner {target} partie(s) aux Slots", "target_range": (1, 3)},
    {"key": "roulette_win", "label": "🎡 Prince de la Roulette", "desc": "Gagner {target} partie(s) à la Roulette", "target_range": (1, 3)},
    {"key": "pfc_win", "label": "✂️ Maître du PFC", "desc": "Gagner {target} partie(s) au PFC", "target_range": (1, 3)},
    {"key": "poker_win", "label": "⚜️ Noble du Poker", "desc": "Gagner {target} partie(s) au Poker", "target_range": (1, 3)},
    {"key": "russian_roulette_survive", "label": "🔫 Survivant", "desc": "Survivre à {target} tir(s) de Roulette Russe", "target_range": (1, 3)},
    {"key": "dice_win", "label": "🎲 Maître des Dés", "desc": "Gagner {target} partie(s) aux Dés", "target_range": (1, 3)},
    {"key": "duel_won", "label": "⚔️ Vainqueur de Duel", "desc": "Gagner {target} duel(s) en PvP", "target_range": (1, 2)},
]


def generate_public_quests():
    chosen = random.sample(QUEST_POOL, k=min(8, len(QUEST_POOL)))
    quests = []
    for q in chosen:
        target = random.randint(*q["target_range"])
        quests.append({
            "key": q["key"],
            "label": q["label"],
            "desc": q["desc"].format(target=target),
            "target": target
        })
    return quests


def get_public_quests():
    today = _today_str()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT quests_json FROM public_quests WHERE quest_date = ?", (today,))
    row = cursor.fetchone()
    
    if row:
        quests = json.loads(row[0])
        conn.close()
        return quests
    
    quests = generate_public_quests()
    cursor.execute(
        "INSERT INTO public_quests (quest_date, quests_json, generated_at) VALUES (?, ?, ?)",
        (today, json.dumps(quests), int(time.time()))
    )
    conn.commit()
    conn.close()
    return quests


def get_player_quests(user_id: int):
    today = _today_str()
    public_quests = get_public_quests()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    player_progress = []
    for q in public_quests:
        cursor.execute(
            "SELECT progress, completed, claimed FROM player_quests WHERE user_id = ? AND quest_date = ? AND quest_key = ?",
            (user_id, today, q["key"])
        )
        row = cursor.fetchone()
        if row:
            progress, completed, claimed = row
        else:
            progress = 0
            completed = 0
            claimed = 0
            cursor.execute(
                "INSERT INTO player_quests (user_id, quest_date, quest_key, progress, completed, claimed) VALUES (?, ?, ?, 0, 0, 0)",
                (user_id, today, q["key"])
            )
            conn.commit()
        
        player_progress.append({
            "key": q["key"],
            "label": q["label"],
            "desc": q["desc"],
            "target": q["target"],
            "progress": progress,
            "completed": completed,
            "claimed": claimed
        })
    
    conn.close()
    return player_progress


def update_player_quest_progress(user_id: int, quest_key: str, amount: int = 1):
    if amount <= 0:
        return
    
    today = _today_str()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT quests_json FROM public_quests WHERE quest_date = ?", (today,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return
    
    public_quests = json.loads(row[0])
    quest_targets = {q["key"]: q["target"] for q in public_quests}
    
    if quest_key not in quest_targets:
        conn.close()
        return
    
    target = quest_targets[quest_key]
    
    cursor.execute("""
        INSERT INTO player_quests (user_id, quest_date, quest_key, progress, completed, claimed)
        VALUES (?, ?, ?, 0, 0, 0)
        ON CONFLICT(user_id, quest_date, quest_key) DO NOTHING
    """, (user_id, today, quest_key))
    
    cursor.execute("""
        UPDATE player_quests 
        SET progress = MIN(progress + ?, ?)
        WHERE user_id = ? AND quest_date = ? AND quest_key = ?
    """, (amount, target, user_id, today, quest_key))
    
    cursor.execute("""
        UPDATE player_quests 
        SET completed = 1 
        WHERE user_id = ? AND quest_date = ? AND quest_key = ? AND progress >= ? AND completed = 0
    """, (user_id, today, quest_key, target))
    
    conn.commit()
    conn.close()


def claim_all_public_quests(user_id: int):
    today = _today_str()
    player_quests = get_player_quests(user_id)
    
    all_completed = all(q["completed"] for q in player_quests)
    already_claimed = all(q["claimed"] for q in player_quests)
    
    if already_claimed:
        return {"already_claimed": True}
    
    if not all_completed:
        return None
    
    base_reward = 500
    total_reward = base_reward
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE player_quests 
        SET claimed = 1 
        WHERE user_id = ? AND quest_date = ?
    """, (user_id, today))
    conn.commit()
    conn.close()
    
    update_wallet(user_id, total_reward)
    asyncio.create_task(check_and_unlock_achievements(user_id, bot))
    
    return {
        "total_reward": total_reward,
        "all_completed": True
    }


def update_quest_progress_v2(user_id: int, quest_key: str, amount: int = 1):
    if amount <= 0:
        return
    update_player_quest_progress(user_id, quest_key, amount)


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


# ==========================================
# 5. ANCIEN SYSTÈME DE QUÊTES
# ==========================================

QUEST_STREAK_MULT_STEP = 0.15
QUEST_STREAK_MULT_MIN = 1.0
QUEST_STREAK_MULT_MAX = 3.0


def get_quest_multiplier(quest_streak: int) -> float:
    mult = QUEST_STREAK_MULT_MIN + (quest_streak * QUEST_STREAK_MULT_STEP)
    return max(QUEST_STREAK_MULT_MIN, min(QUEST_STREAK_MULT_MAX, mult))


def get_quest_reward_state(user_id: int):
    today = _today_str()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT base_reward, quest_date, quest_streak, last_claim_date FROM quest_reward_state WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()

        if row is None:
            base_reward = random.randint(50, 200)
            cursor.execute(
                "INSERT INTO quest_reward_state (user_id, base_reward, quest_date, quest_streak, last_claim_date) VALUES (?, ?, ?, 0, '')",
                (user_id, base_reward, today),
            )
            conn.commit()
            return base_reward, 0, ''

        base_reward, quest_date, quest_streak, last_claim_date = row

        if quest_date != today:
            base_reward = random.randint(50, 200)
            cursor.execute(
                "UPDATE quest_reward_state SET base_reward = ?, quest_date = ? WHERE user_id = ?",
                (base_reward, today, user_id),
            )
            conn.commit()

        return base_reward, (quest_streak or 0), (last_claim_date or '')


def get_daily_quests(user_id: int):
    today = _today_str()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT quest_key, description, target, progress, reward, claimed"
            " FROM daily_quests WHERE user_id = ? AND quest_date = ? ORDER BY quest_key",
            (user_id, today),
        )
        rows = cursor.fetchall()

        if rows:
            return [
                {
                    "key": r[0], "description": r[1], "target": r[2],
                    "progress": r[3], "reward": r[4], "claimed": bool(r[5]),
                }
                for r in rows
            ]

        quest_pool = [
            {"key": "games_played", "desc_tpl": "Jouer {target} partie(s) dans un jeu de casino", "target_range": (3, 6)},
            {"key": "games_won", "desc_tpl": "Gagner {target} partie(s) dans n'importe quel jeu", "target_range": (1, 3)},
            {"key": "work_done", "desc_tpl": "Travailler {target} fois via /work", "target_range": (1, 3)},
            {"key": "arena_fight", "desc_tpl": "Affronter Bob dans l'arène {target} fois", "target_range": (1, 2)},
            {"key": "duel_played", "desc_tpl": "Faire {target} duel(s) PvP", "target_range": (1, 2)},
            {"key": "bank_deposit", "desc_tpl": "Déposer à la banque {target} fois", "target_range": (1, 3)},
            {"key": "pay_sent", "desc_tpl": "Envoyer de l'argent via /pay {target} fois", "target_range": (1, 2)},
            {"key": "crime_attempt", "desc_tpl": "Tenter un crime chez John {target} fois", "target_range": (1, 3)},
            {"key": "pmu_bet", "desc_tpl": "Parier chez Brook {target} fois", "target_range": (1, 3)},
            {"key": "vault_attempt", "desc_tpl": "Braquer la Brinks {target} fois", "target_range": (1, 2)},
            {"key": "money_earned", "desc_tpl": "Gagner un total de {target} $", "target_range": (500, 1500)},
            {"key": "beer_drunk", "desc_tpl": "Commander {target} pinte(s) chez Jim", "target_range": (1, 3)},
        ]
        chosen = random.sample(quest_pool, k=min(5, len(quest_pool)))
        quests = []
        for q in chosen:
            target = random.randint(*q["target_range"])
            description = q["desc_tpl"].format(target=target)
            cursor.execute(
                "INSERT OR IGNORE INTO daily_quests"
                " (user_id, quest_date, quest_key, description, target, progress, reward, claimed)"
                " VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
                (user_id, today, q["key"], description, target),
            )
            quests.append({
                "key": q["key"], "description": description, "target": target,
                "progress": 0, "reward": 0, "claimed": False,
            })
        conn.commit()
        return quests


def update_quest_progress(user_id: int, quest_key: str, amount: int = 1):
    if amount <= 0:
        return
    get_daily_quests(user_id)
    today = _today_str()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE daily_quests SET progress = MIN(target, progress + ?)"
            " WHERE user_id = ? AND quest_date = ? AND quest_key = ? AND claimed = 0",
            (amount, user_id, today, quest_key),
        )
        conn.commit()


def claim_all_daily_quests(user_id: int):
    today = _today_str()
    quests = get_daily_quests(user_id)
    if not quests:
        return None
    if any(q["claimed"] for q in quests):
        return {"already_claimed": True}
    if not all(q["progress"] >= q["target"] for q in quests):
        return None

    with get_db_connection() as conn:
        cursor = conn.cursor()
        base_reward, quest_streak, last_claim_date = get_quest_reward_state(user_id)

        yesterday = (datetime.datetime.strptime(today, "%Y-%m-%d").date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if last_claim_date == yesterday:
            quest_streak += 1
        elif last_claim_date != today:
            quest_streak = 1

        multiplier = get_quest_multiplier(quest_streak)
        total_reward = round(base_reward * multiplier)

        cursor.execute(
            "UPDATE daily_quests SET claimed = 1 WHERE user_id = ? AND quest_date = ?",
            (user_id, today),
        )
        cursor.execute("""
            INSERT INTO quest_reward_state (user_id, base_reward, quest_date, quest_streak, last_claim_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                base_reward = excluded.base_reward,
                quest_date = excluded.quest_date,
                quest_streak = excluded.quest_streak,
                last_claim_date = excluded.last_claim_date
        """, (user_id, base_reward, today, quest_streak, today))
        conn.commit()

    update_wallet(user_id, total_reward)
    asyncio.create_task(check_and_unlock_achievements(user_id, bot))

    return {
        "base_reward": base_reward,
        "multiplier": multiplier,
        "quest_streak": quest_streak,
        "total_reward": total_reward,
    }


# ==========================================
# 6. SYSTÈME DES ACHIEVEMENTS (STYLE MEE6 - SIMPLIFIÉ)
# ==========================================

TIERS_NAMES = {1: "Bronze"}
TIERS_COLORS = {1: "#CD7F32"}

ACHIEVEMENTS_DEFS = {}
ACHIEVEMENTS_LOADED = False

GITHUB_ACHIEVEMENTS_URL = "https://raw.githubusercontent.com/morgandede0-cyber/Bot-IV-Alpha/main/achievements_list.json"


async def load_achievements_from_github():
    global ACHIEVEMENTS_DEFS, ACHIEVEMENTS_LOADED
    
    # TES achievements (fallback)
    FALLBACK_ACHIEVEMENTS = {
        "quest_bapteme": {
            "key": "quest_bapteme",
            "title": "Baptême du Feu",
            "desc": "Déclencher sa toute première quête journalière.",
            "thresholds": {"1": 1},
            "rewards": {"1": 100},
            "category": "Quêtes",
            "tier": "Commun"
        },
        "quest_habitué": {
            "key": "quest_habitué",
            "title": "Habitué des Missions",
            "desc": "Valider un total de 10 quêtes journalières.",
            "thresholds": {"1": 10},
            "rewards": {"1": 200},
            "category": "Quêtes",
            "tier": "Rare"
        },
        "quest_veteran": {
            "key": "quest_veteran",
            "title": "Vétéran des Commandes",
            "desc": "Compléter 50 quêtes journalières avec succès.",
            "thresholds": {"1": 50},
            "rewards": {"1": 500},
            "category": "Quêtes",
            "tier": "Épique"
        },
        "quest_seigneur": {
            "key": "quest_seigneur",
            "title": "Seigneur des Chroniques",
            "desc": "Venir à bout de 150 quêtes journalières.",
            "thresholds": {"1": 150},
            "rewards": {"1": 1000},
            "category": "Quêtes",
            "tier": "Mythique"
        },
        "quest_legende": {
            "key": "quest_legende",
            "title": "Légende de l'Aventure",
            "desc": "Atteindre le palier ultime de 500 quêtes journalières.",
            "thresholds": {"1": 500},
            "rewards": {"1": 2500},
            "category": "Quêtes",
            "tier": "Légendaire"
        },
        "arena_essai": {
            "key": "arena_essai",
            "title": "Coup d'Essai",
            "desc": "Effectuer son premier combat d'entraînement dans l'arène.",
            "thresholds": {"1": 1},
            "rewards": {"1": 100},
            "category": "Arène",
            "tier": "Commun"
        },
        "arena_assidu": {
            "key": "arena_assidu",
            "title": "Combattant Assidu",
            "desc": "Disputer 10 duels d'entraînement dans l'arène.",
            "thresholds": {"1": 10},
            "rewards": {"1": 200},
            "category": "Arène",
            "tier": "Rare"
        },
        "arena_guerrier": {
            "key": "arena_guerrier",
            "title": "Guerrier de l'Arène",
            "desc": "Remporter 25 combats face au maître d'arme.",
            "thresholds": {"1": 25},
            "rewards": {"1": 500},
            "category": "Arène",
            "tier": "Épique"
        },
        "arena_champion": {
            "key": "arena_champion",
            "title": "Champion des Lices",
            "desc": "Triompher lors de 75 affrontements dans l'arène.",
            "thresholds": {"1": 75},
            "rewards": {"1": 1000},
            "category": "Arène",
            "tier": "Mythique"
        },
        "arena_terreur": {
            "key": "arena_terreur",
            "title": "Terreur des Gladiateurs",
            "desc": "Dominer l'arène en décrochant 200 victoires.",
            "thresholds": {"1": 200},
            "rewards": {"1": 2500},
            "category": "Arène",
            "tier": "Légendaire"
        }
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GITHUB_ACHIEVEMENTS_URL, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict) and data:
                        ACHIEVEMENTS_DEFS = data
                        ACHIEVEMENTS_LOADED = True
                        print(f"✅ {len(ACHIEVEMENTS_DEFS)} succès chargés depuis GitHub")
                        return True
                    else:
                        print("⚠️ Fichier JSON invalide ou vide, utilisation du fallback")
                        ACHIEVEMENTS_DEFS = FALLBACK_ACHIEVEMENTS
                        ACHIEVEMENTS_LOADED = True
                        return False
                else:
                    print(f"⚠️ Erreur HTTP {response.status}, utilisation du fallback")
                    ACHIEVEMENTS_DEFS = FALLBACK_ACHIEVEMENTS
                    ACHIEVEMENTS_LOADED = True
                    return False
    except Exception as e:
        print(f"❌ Erreur chargement succès: {e}, utilisation du fallback")
        ACHIEVEMENTS_DEFS = FALLBACK_ACHIEVEMENTS
        ACHIEVEMENTS_LOADED = True
        return False


def evaluate_stat_for_achievement(ach_key: str, user_id: int) -> int:
    """Évalue la progression d'un joueur pour un achievement - Style MEE6"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Récupérer toutes les stats du joueur en une seule requête
        cursor.execute("""
            SELECT 
                games_played, games_won, games_lost,
                beers_today, wallet, bank
            FROM users WHERE user_id = ?
        """, (user_id,))
        
        row = cursor.fetchone()
        if not row:
            return 0
        
        games_played, games_won, games_lost, beers_today, wallet, bank = row
        games_played = games_played or 0
        games_won = games_won or 0
        beers_today = beers_today or 0
        wallet = wallet or 0
        bank = bank or 0
        
        # Compter les quêtes réclamées
        cursor.execute("SELECT COUNT(*) FROM player_quests WHERE user_id = ? AND claimed = 1", (user_id,))
        player_quests = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND claimed = 1", (user_id,))
        daily_quests = cursor.fetchone()[0] or 0
        total_quests = player_quests + daily_quests
        
        # === MAPPING DES ACHIEVEMENTS ===
        
        # Quêtes (quest_*)
        if ach_key.startswith("quest_"):
            return total_quests
        
        # Arène (arena_*)
        if ach_key.startswith("arena_"):
            if "essai" in ach_key or "assidu" in ach_key:
                return games_played
            return games_won
        
        # PMU (pmu_*) - utilise games_won comme proxy
        if ach_key.startswith("pmu_"):
            return games_won
        
        # Crime (crime_*) - utilise games_played comme proxy
        if ach_key.startswith("crime_"):
            return games_played
        
        # Brinks (vault_*) - utilise games_played comme proxy
        if ach_key.startswith("vault_"):
            return games_played
        
        # Duel (duel_*)
        if ach_key.startswith("duel_"):
            if "premier" in ach_key or "bretteur" in ach_key:
                return games_played
            return games_won
        
        # Daily (daily_*)
        if ach_key.startswith("daily_"):
            return total_quests
        
        # Taverne (taverne_*)
        if ach_key.startswith("taverne_"):
            return beers_today
        
        # Banque (bank_*) - utilise games_played comme proxy
        if ach_key.startswith("bank_"):
            return games_played
        
        # Larcin (larcin_*) - utilise games_played comme proxy
        if ach_key.startswith("larcin_"):
            return games_played
        
        return 0


async def check_and_unlock_achievements(user_id: int, bot_client=None) -> list:
    global ACHIEVEMENTS_DEFS, ACHIEVEMENTS_LOADED
    
    if not ACHIEVEMENTS_LOADED:
        await load_achievements_from_github()
    
    if not ACHIEVEMENTS_DEFS:
        return []
    
    today = time.strftime("%Y-%m-%d")
    unlocked_now = []

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT achievement_key FROM user_achievements WHERE user_id = ?", (user_id,))
        unlocked_keys = {row[0] for row in cursor.fetchall()}

    for ach_key, data in ACHIEVEMENTS_DEFS.items():
        if ach_key in unlocked_keys:
            continue

        current_stat = evaluate_stat_for_achievement(ach_key, user_id)
        threshold = data["thresholds"]["1"]
        
        if current_stat >= threshold:
            reward_sum = data["rewards"]["1"]
            
            update_wallet(user_id, reward_sum)

            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO user_achievements (user_id, achievement_key, tier, unlocked_at) VALUES (?, ?, ?, ?)",
                    (user_id, ach_key, 1, today)
                )
                conn.commit()

            unlocked_now.append({
                "key": ach_key,
                "title": data["title"],
                "reward": reward_sum
            })

            # Notification dans le salon B
            if bot_client:
                try:
                    channel = bot_client.get_channel(PUBLIC_LOG_CHANNEL_ID)
                    if channel:
                        user = bot_client.get_user(user_id)
                        mention = user.mention if user else f"<@{user_id}>"
                        await channel.send(f"🎉 {mention} a débloqué le succès **{data['title']}** ! (+{format_currency(reward_sum)})")
                except Exception as e:
                    print(f"❌ Erreur notification: {e}")

    return unlocked_now


# ==========================================
# 7. COMMANDES D'ACHIEVEMENTS AVEC TESTS
# ==========================================

@bot.tree.command(name="reload-achievements", description="[ADMIN] Recharge la liste des succès depuis GitHub")
@app_commands.checks.has_permissions(administrator=True)
async def reload_achievements(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    success = await load_achievements_from_github()
    if success:
        embed = discord.Embed(
            title="✅ Succès rechargés",
            description=f"{len(ACHIEVEMENTS_DEFS)} succès chargés depuis GitHub avec succès !",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="⚠️ Rechargement partiel",
            description="Les succès ont été chargés depuis le fallback local.",
            color=discord.Color.orange()
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="achievements-list", description="Affiche la liste complète des succès disponibles")
async def achievements_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    if not ACHIEVEMENTS_LOADED:
        await load_achievements_from_github()
    
    if not ACHIEVEMENTS_DEFS:
        return await interaction.followup.send("❌ Aucun succès chargé.", ephemeral=True)
    
    embed = discord.Embed(
        title="🏆 Liste des Succès Disponibles",
        color=discord.Color.gold()
    )
    
    descriptions = []
    for ach_key, data in ACHIEVEMENTS_DEFS.items():
        threshold = data["thresholds"]["1"]
        reward = data["rewards"]["1"]
        descriptions.append(f"**{data['title']}** - {data['desc']} (Seuil: {threshold}, Récompense: {reward}$)")
    
    embed.description = "\n".join(descriptions[:20])
    if len(descriptions) > 20:
        embed.set_footer(text=f"Total: {len(ACHIEVEMENTS_DEFS)} succès • 20 affichés")
    else:
        embed.set_footer(text=f"Total: {len(ACHIEVEMENTS_DEFS)} succès")
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="achievements", description="Affiche tes succès et trophées sous forme de carte graphique")
async def achievements(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT achievement_key, tier FROM user_achievements WHERE user_id = ?", (user_id,))
        unlocked = {row[0]: row[1] for row in cursor.fetchall()}

    img_buf = await generate_mee6_profile_card(interaction.user, unlocked)
    file = discord.File(fp=img_buf, filename="achievements_profile.png")

    view = AchievementProfileView(interaction.user, unlocked)
    await interaction.followup.send(file=file, view=view, ephemeral=True)


@bot.tree.command(name="test-achievements", description="[ADMIN] Test complet des achievements")
@app_commands.checks.has_permissions(administrator=True)
async def test_achievements(interaction: discord.Interaction, membre: discord.Member):
    await interaction.response.defer(ephemeral=True)
    
    embed = discord.Embed(
        title=f"🔍 Test Achievements - {membre.display_name}",
        color=discord.Color.blue()
    )
    
    # 1. Vérifier le chargement
    embed.add_field(
        name="📊 Chargement",
        value=f"Achievements chargés: {len(ACHIEVEMENTS_DEFS)}\nACHIEVEMENTS_LOADED: {ACHIEVEMENTS_LOADED}",
        inline=False
    )
    
    # 2. Stats du joueur
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT games_played, games_won, beers_today, wallet, bank FROM users WHERE user_id = ?", (membre.id,))
        row = cursor.fetchone()
        
        if row:
            games_played, games_won, beers_today, wallet, bank = row
            embed.add_field(
                name="📊 Stats du joueur",
                value=f"Parties jouées: **{games_played}**\nParties gagnées: **{games_won}**\nBières: **{beers_today}**\nWallet: **{format_currency(wallet)}**\nBank: **{format_currency(bank)}**",
                inline=False
            )
        
        # Quêtes réclamées
        cursor.execute("SELECT COUNT(*) FROM player_quests WHERE user_id = ? AND claimed = 1", (membre.id,))
        player_quests = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND claimed = 1", (membre.id,))
        daily_quests = cursor.fetchone()[0] or 0
        embed.add_field(
            name="📋 Quêtes réclamées",
            value=f"Player_quests: **{player_quests}**\nDaily_quests: **{daily_quests}**\nTotal: **{player_quests + daily_quests}**",
            inline=False
        )
    
    # 3. Achievements débloqués
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT achievement_key FROM user_achievements WHERE user_id = ?", (membre.id,))
        unlocked = cursor.fetchall()
        
        if unlocked:
            embed.add_field(
                name="🏆 Débloqués",
                value="\n".join([f"✅ {row[0]}" for row in unlocked[:10]]),
                inline=False
            )
        else:
            embed.add_field(
                name="🏆 Débloqués",
                value="❌ Aucun",
                inline=False
            )
    
    # 4. Forcer la vérification
    embed.add_field(
        name="🔄 Action",
        value=f"Exécute `/force-check @{membre.display_name}` pour forcer la vérification",
        inline=False
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="force-check", description="[ADMIN] Force la vérification des achievements")
@app_commands.checks.has_permissions(administrator=True)
async def force_check(interaction: discord.Interaction, membre: discord.Member):
    await interaction.response.defer(ephemeral=True)
    
    unlocked = await check_and_unlock_achievements(membre.id, bot)
    
    if unlocked:
        embed = discord.Embed(
            title="✅ Achievements vérifiés",
            description=f"{len(unlocked)} nouveau(x) succès débloqué(s) !",
            color=discord.Color.green()
        )
        for ach in unlocked:
            embed.add_field(
                name=ach["title"],
                value=f"Récompense : {format_currency(ach['reward'])}",
                inline=False
            )
    else:
        embed = discord.Embed(
            title="ℹ️ Aucun succès débloqué",
            description="Le joueur n'a pas atteint les conditions.",
            color=discord.Color.blue()
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="reset-achievements", description="[ADMIN] Réinitialise définitivement les succès d'un joueur")
@app_commands.checks.has_permissions(administrator=True)
async def reset_achievements(interaction: discord.Interaction, membre: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if membre:
            cursor.execute("DELETE FROM user_achievements WHERE user_id = ?", (membre.id,))
            msg = f"✅ Tous les succès de {membre.mention} ont été effacés définitivement !"
        else:
            cursor.execute("DELETE FROM user_achievements")
            msg = "✅ Les succès de **tous les joueurs** du serveur ont été effacés définitivement !"
        conn.commit()

    await interaction.followup.send(msg, ephemeral=True)


# ==========================================
# 8. GÉNÉRATION DE LA CARTE DE PROFIL MEE6
# ==========================================

async def generate_mee6_profile_card(member: discord.Member, unlocked_achievements: dict) -> io.BytesIO:
    width, height = 740, 230
    img = Image.new("RGBA", (width, height), (24, 25, 28, 255))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([0, 0, width, height], radius=12, fill="#18191C", outline="#F1C40F", width=2)

    avatar_img = None
    try:
        if member.avatar:
            asset = member.avatar.replace(size=128, format="png")
            avatar_bytes = await asset.read()
            data = io.BytesIO(avatar_bytes)
            avatar_img = Image.open(data).convert("RGBA").resize((80, 80), Image.Resampling.LANCZOS)
    except Exception:
        pass

    if avatar_img is None:
        avatar_img = Image.new("RGBA", (80, 80), (50, 50, 60, 255))

    mask = Image.new("L", (80, 80), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, 80, 80), fill=255)
    img.paste(avatar_img, (25, 25), mask=mask)

    try:
        font_name = ImageFont.truetype("arialbd.ttf", 22)
        font_sub = ImageFont.truetype("arial.ttf", 13)
        font_header = ImageFont.truetype("arialbd.ttf", 11)
        font_badge_tier = ImageFont.truetype("arialbd.ttf", 10)
    except IOError:
        font_name = font_sub = font_header = font_badge_tier = ImageFont.load_default()

    display_name = member.display_name
    draw.text((120, 28), display_name, fill="#FFFFFF", font=font_name)
    draw.text((120, 58), "• IV • | Membre des Sceaux", fill="#949BA4", font=font_sub)

    total_unlocked = len(unlocked_achievements)
    total_available = len(ACHIEVEMENTS_DEFS) if ACHIEVEMENTS_LOADED else 50
    draw.text((120, 82), f"Achievements unlocked  {total_unlocked} | {total_available}", fill="#B5BAC1", font=font_sub)

    draw.text((25, 118), "ACHIEVEMENTS", fill="#80848E", font=font_header)

    start_x, start_y = 25, 142
    spacing = 65
    max_badges = 9

    idx = 0
    for ach_key, tier in unlocked_achievements.items():
        if idx >= max_badges:
            break
        bx = start_x + (idx * spacing)
        by = start_y

        color = TIERS_COLORS.get(tier, "#CD7F32")
        tier_name = TIERS_NAMES.get(tier, "Bronze")

        poly_points = [(bx + 22, by), (bx + 44, by + 12), (bx + 44, by + 38), (bx + 22, by + 50), (bx, by + 38), (bx, by + 12)]
        draw.polygon(poly_points, fill="#0F151D", outline=color)

        draw.rounded_rectangle([bx + 4, by + 42, bx + 40, by + 58], radius=4, fill="#232428")
        draw.text((bx + 22, by + 50), tier_name[:3].upper(), fill="#FFFFFF", font=font_badge_tier, anchor="mm")

        idx += 1

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


class AchievementProfileView(ui.View):
    def __init__(self, member: discord.Member, unlocked_achievements: dict):
        super().__init__(timeout=60)
        self.add_item(ui.Button(label="Liste des succès", style=discord.ButtonStyle.link, url="https://listeachievementiv.netlify.app/", emoji="📜"))


# ==========================================
# 9. VUE POUR LE PANNEAU PUBLIC DES QUÊTES
# ==========================================

class PublicQuestsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📋 Voir ma progression", style=discord.ButtonStyle.primary, custom_id="quests_show_progress", emoji="📊")
    async def show_progress(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        player_quests = get_player_quests(interaction.user.id)
        today = _today_str()
        
        embed = discord.Embed(
            title=f"📋 Mes Quêtes du {today}",
            color=discord.Color.blue()
        )
        
        all_completed = all(q["completed"] for q in player_quests)
        all_claimed = all(q["claimed"] for q in player_quests)
        
        description = "Voici l'avancement de tes quêtes d'aujourd'hui :\n\n"
        
        for q in player_quests:
            if q["claimed"]:
                status = "✅ Réclamé"
            elif q["completed"]:
                status = "🎯 Terminé !"
            else:
                progress_bar = "▰" * int((q["progress"] / q["target"]) * 10) + "▱" * (10 - int((q["progress"] / q["target"]) * 10))
                status = f"{progress_bar} {q['progress']}/{q['target']}"
            
            description += f"**{q['label']}**\n{q['desc']}\n`{status}`\n\n"
        
        if all_completed and not all_claimed:
            description += "\n🎉 **TOUTES LES QUÊTES SONT TERMINÉES !**\nClique sur le bouton ci-dessus pour réclamer ta récompense !"
        elif all_claimed:
            description += "\n✅ **Récompense déjà réclamée aujourd'hui !** Reviens demain pour de nouvelles quêtes."
        
        embed.description = description
        embed.set_footer(text=f"8 quêtes à valider • Récompense : 500$")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ui.button(label="🎁 Réclamer la récompense", style=discord.ButtonStyle.success, custom_id="quests_claim_reward", emoji="🎁")
    async def claim_reward(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        result = claim_all_public_quests(interaction.user.id)
        
        if result is None:
            await interaction.followup.send("❌ Toutes les quêtes doivent être terminées pour réclamer la récompense !", ephemeral=True)
            return
        
        if result.get("already_claimed"):
            await interaction.followup.send("❌ Tu as déjà réclamé ta récompense aujourd'hui !", ephemeral=True)
            return
        
        await interaction.followup.send(f"🎉 **FÉLICITATIONS !** Tu as validé toutes les quêtes du jour et remporté **{format_currency(result['total_reward'])}** ! 🎉", ephemeral=True)


# ==========================================
# 10. MODALES DE MISE
# ==========================================

class BetModal(ui.Modal):
    def __init__(self, title_name: str, callback_game):
        super().__init__(title=title_name)
        self.callback_game = callback_game

        self.bet_input = ui.TextInput(
            label="Montant de la mise",
            placeholder=f"Entrez un montant (Max: {MAX_BET}$)",
            required=True,
            max_length=6
        )
        self.add_item(self.bet_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            bet_amount = int(self.bet_input.value)
        except ValueError:
            return await interaction.followup.send("❌ Veuillez entrer un nombre entier valide.", ephemeral=True)

        try:
            await self.callback_game(interaction, bet_amount)
        except Exception as e:
            print(f"❌ Erreur pendant le lancement du jeu : {type(e).__name__}: {e}")
            traceback.print_exc()
            await interaction.followup.send(
                "❌ Une erreur est survenue pendant le lancement du jeu.",
                ephemeral=True
            )


class PMUBetModal(ui.Modal, title="🏁 PMU - Choix du cheval et mise"):
    cheval_input = ui.TextInput(label="Numéro du cheval (1 à 4)", placeholder="Ex: 2", required=True, max_length=1)
    bet_input = ui.TextInput(label="Montant de la mise", placeholder="Ex: 100", required=True, max_length=6)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            cheval = int(self.cheval_input.value)
            bet_amount = int(self.bet_input.value)
        except ValueError:
            return await interaction.followup.send("❌ Valeurs invalides.", ephemeral=True)

        if cheval not in [1, 2, 3, 4]:
            return await interaction.followup.send("❌ Choisis un cheval entre 1 et 4 !", ephemeral=True)

        await run_pmu_game(interaction, cheval, bet_amount)


class BrookPMUBetModal(ui.Modal, title="📜 Brook - Montant de la mise"):
    bet_input = ui.TextInput(label="Montant de la mise", placeholder="Ex: 100", required=True, max_length=6)

    def __init__(self, horse_choice: int, dynamic_odds: dict, panel_message=None):
        super().__init__()
        self.horse_choice = horse_choice
        self.dynamic_odds = dynamic_odds
        self.panel_message = panel_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            bet_amount = int(self.bet_input.value)
        except ValueError:
            return await interaction.followup.send("❌ Veuillez entrer un montant valide.", ephemeral=True)

        await run_brook_pmu_game(
            interaction, self.horse_choice, bet_amount, self.dynamic_odds,
            panel_message=self.panel_message
        )


# ==========================================
# 11. COMMANDES D'ÉCONOMIE, BANQUE & ADMIN
# ==========================================

@bot.tree.command(name="banque", description="Accéder au Distributeur Automatique de Billets (DAB)")
async def banque(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    file_bank = discord.File("assets/bank.png", filename="bank.png") if os.path.exists("assets/bank.png") else None
    embed = discord.Embed(
        title="🏦 Banque des IV Sceaux",
        description=(
            "*(Un grincement lourd résonne dans la salle forte)*\n\n"
            "Bienvenue au guichet automatique de la **Banque des IV Sceaux**.\n"
            "Gérez vos avoirs en toute sécurité, déposez vos liquidités ou effectuez des retraits "
            "pour alimenter votre portefeuille avant de vous aventurer dans les jeux."
        ),
        color=0x34495E
    )
    if file_bank:
        embed.set_image(url="attachment://bank.png")
    embed.set_footer(text="Banque des IV Sceaux • Service Financier Permanent")

    kwargs = {"embed": embed, "view": BankView(), "ephemeral": True}
    if file_bank:
        kwargs["file"] = file_bank
    await interaction.followup.send(**kwargs)


class BankView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="[ 💳 SOLDE ]", style=discord.ButtonStyle.primary, custom_id="persistent_bank:solde")
    async def check_balance(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        wallet, bank, _, _, _, _, _, _, _ = get_user(interaction.user.id)
        total = wallet + bank
        name = interaction.user.display_name.upper()[:16]

        atm_screen = (
            "```text\n"
            "┌────────────────────────┐\n"
            "│ Banque des IV Sceaux   │\n"
            "├────────────────────────┤\n"
            f"│ TITULAIRE : {name:<10} │\n"
            "├────────────────────────┤\n"
            f"│ PORT. : {format_currency(wallet):>14} │\n"
            f"│ BANQUE: {format_currency(bank):>14} │\n"
            "│ ────────────────────── │\n"
            f"│ TOTAL : {format_currency(total):>14} │\n"
            "└────────────────────────┘\n"
            "```"
        )

        embed = discord.Embed(
            title="💳 RELEVÉ", description=atm_screen, color=0x2B2D31
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ui.button(label="[ 📥 DÉPÔT ]", style=discord.ButtonStyle.success, custom_id="persistent_bank:depot")
    async def deposit(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DepositModal())

    @ui.button(label="[ 📤 RETRAIT ]", style=discord.ButtonStyle.danger, custom_id="persistent_bank:retrait")
    async def withdraw(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(WithdrawModal())


class DepositModal(ui.Modal, title="📥 DAB - Dépôt de billets"):
    amount = ui.TextInput(
        label="Montant à déposer", placeholder="Ex: 500", required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            val = int(self.amount.value)
            if val <= 0:
                return await interaction.followup.send(
                    "❌ Le montant doit être supérieur à 0.", ephemeral=True
                )

            wallet, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
            if wallet < val:
                return await interaction.followup.send(
                    "❌ Fente à billets : Solde insuffisant dans votre portefeuille.",
                    ephemeral=True,
                )

            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET wallet = wallet - ?, bank = bank + ? WHERE"
                    " user_id = ?",
                    (val, val, interaction.user.id),
                )
                conn.commit()

            if REDIS_AVAILABLE and redis_client:
                redis_client.delete(f"user:{interaction.user.id}")

            update_quest_progress(interaction.user.id, "bank_deposit", 1)
            update_quest_progress_v2(interaction.user.id, "bank_deposit", 1)
            await check_and_unlock_achievements(interaction.user.id, bot_client=bot)

            await send_public_log(
                content=f"💵 **{interaction.user.display_name}** a déposé **{format_currency(val)}** à la banque !"
            )

            await interaction.followup.send(
                f"💵 **[DÉPÔT EFFECTUÉ]** +{format_currency(val)} ont été insérés sur"
                " votre compte.",
                ephemeral=True,
            )
        except ValueError:
            await interaction.followup.send(
                "❌ Veuillez entrer un nombre entier valide.", ephemeral=True
            )


class WithdrawModal(ui.Modal, title="📤 DAB - Retrait de billets"):
    amount = ui.TextInput(
        label="Montant à retirer", placeholder="Ex: 500", required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            val = int(self.amount.value)
            if val <= 0:
                return await interaction.followup.send(
                    "❌ Le montant doit être supérieur à 0.", ephemeral=True
                )

            _, bank, _, _, _, _, _, _, _ = get_user(interaction.user.id)
            if bank < val:
                return await interaction.followup.send(
                    "❌ Solde bancaire insuffisant.", ephemeral=True
                )

            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET bank = bank - ?, wallet = wallet + ? WHERE"
                    " user_id = ?",
                    (val, val, interaction.user.id),
                )
                conn.commit()

            if REDIS_AVAILABLE and redis_client:
                redis_client.delete(f"user:{interaction.user.id}")

            await check_and_unlock_achievements(interaction.user.id, bot_client=bot)

            await interaction.followup.send(
                f"💸 **[BILLETS DISTRIBUÉS]** Veuillez récupérer vos"
                f" {format_currency(val)}.",
                ephemeral=True,
            )
        except ValueError:
            await interaction.followup.send(
                "❌ Veuillez entrer un nombre entier valide.", ephemeral=True
            )


@bot.tree.command(name="daily", description="Réclame ta récompense quotidienne")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    user_id = interaction.user.id
    retry_after = check_cooldown(user_id, "daily", 86400)
    if retry_after > 0:
        hours, remainder = divmod(retry_after, 3600)
        minutes, seconds = divmod(remainder, 60)
        return await interaction.followup.send(f"⏳ Déjà réclamé ! Reviens dans **{hours}h {minutes}m {seconds}s**.", ephemeral=True)

    _, _, last_daily, streak, _, _, _, _, _ = get_user(user_id)
    now = int(time.time())

    time_passed = now - last_daily
    reset_streak = False
    if last_daily == 0 or time_passed > 172800:
        streak = 1
        reset_streak = last_daily != 0
    else:
        streak += 1

    base_coins = random.randint(100, 500)
    multiplier = 1 + ((streak - 1) * 0.10)
    total_reward = min(int(base_coins * multiplier), 2500)

    update_wallet(user_id, total_reward)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET last_daily = ?, streak = ? WHERE user_id = ?", (now, streak, user_id))
        conn.commit()
    
    if REDIS_AVAILABLE and redis_client:
        redis_client.delete(f"user:{user_id}")

    embed = discord.Embed(title="🎁 Daily", description=f"Tu as reçu **{format_currency(total_reward)}** !", color=discord.Color.blurple())
    embed.add_field(name="🔥 Série", value=f"**{streak}j**", inline=False)
    if reset_streak:
        embed.add_field(name="⚠️ Réinitialisé", value="> 48h écoulées.", inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="work", description="Gagne un peu d'argent en travaillant")
async def work(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    user_id = interaction.user.id
    retry_after = check_cooldown(user_id, "work", 3600)
    if retry_after > 0:
        minutes, seconds = divmod(retry_after, 60)
        await interaction.followup.send(
            f"⏳ Tu dois attendre **{minutes} min et {seconds} sec** avant de pouvoir"
            " travailler à nouveau.",
            ephemeral=True,
        )
        return

    if random.randint(1, 10) == 1:
        await interaction.followup.send(
            "🏛️ **Contrôle URSSAF !** Zéro déclaré , zéro pointé."
        )
        return

    if random.randint(1, 10) == 1:
        await interaction.followup.send(
            "🏛️ **Inspection du travail !** Ton patron ne t'a pas déclaré, tu n'es donc pas payé."
        )
        return

    gain = random.randint(100, 500)
    update_wallet(user_id, gain)
    update_quest_progress(user_id, "work_done", 1)
    update_quest_progress_v2(user_id, "work_done", 1)
    await check_and_unlock_achievements(user_id, bot_client=bot)

    await send_public_log(
        content=f"💼 **{interaction.user.display_name}** a travaillé et gagné **{format_currency(gain)}** !"
    )

    jobs = [
        f"Tu as réparé le PC d'un voisin et gagné **{format_currency(gain)}** !",
        f"Tu as modéré le serveur Discord toute la journée et reçu une prime de **{format_currency(gain)}** !",
        f"Tu as gagné un petit tournoi de cartes et empoché **{format_currency(gain)}** !",
        f"Tu as monté un canapé au 6ème sans ascenseur pour **{format_currency(gain)}** !",
        f"Tu as passé la nuit à effacer les preuves d'une gaffe monumentale d'un modo saoul pour **{format_currency(gain)}** !",
        f"Tu as fait du chantage à un modo en menaçant de leak ses pires audios et il t'a payé **{format_currency(gain)}** !",
        f"Tu as livré les pizzas dans le quartier pour **{format_currency(gain)}** !",
        f"Tu as tondu la pelouse du voisin pour **{format_currency(gain)}** !",
        f"Tu as fait le service du midi dans un restaurant bondé pour **{format_currency(gain)}** !",
        f"Tu as nettoyé les vitres du bureau local pour **{format_currency(gain)}** !",
    ]
    await interaction.followup.send(random.choice(jobs), ephemeral=True)


@bot.tree.command(name="pay", description="Envoie de l'argent à un autre membre")
async def pay(interaction: discord.Interaction, receiver: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0:
        return await interaction.followup.send("❌ Montant invalide.", ephemeral=True)

    sender_wallet, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
    if sender_wallet < amount:
        return await interaction.followup.send("❌ Solde insuffisant.", ephemeral=True)

    update_wallet(interaction.user.id, -amount)
    update_wallet(receiver.id, amount)
    update_quest_progress(interaction.user.id, "pay_sent", 1)
    update_quest_progress_v2(interaction.user.id, "pay_sent", 1)
    
    await send_public_log(
        content=f"💸 **{interaction.user.display_name}** a envoyé **{format_currency(amount)}** à {receiver.display_name} !"
    )
    
    await interaction.followup.send(f"💸 {interaction.user.mention} ➔ **{format_currency(amount)}** à {receiver.mention} !", ephemeral=True)


@bot.tree.command(name="richest", description="Affiche l'économie globale du serveur et le classement exact")
async def richest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    
    if REDIS_AVAILABLE and redis_client:
        top = redis_client.zrevrange("leaderboard:richest", 0, 9, withscores=True)
        if top:
            embed = discord.Embed(title="🏆 Classement", color=0xF1C40F)
            description = ["🌐 **Global** : `Classement Redis`", "──────────────"]
            medals = ["🥇", "🥈", "🥉"]
            
            for index, (user_id, score) in enumerate(top, start=1):
                member = interaction.guild.get_member(int(user_id)) if interaction.guild else None
                user_name = member.mention if member else f"<@{user_id}>"
                rank_icon = medals[index - 1] if index <= 3 else f"`#{index:02d}`"
                description.append(f"{rank_icon} {user_name} ➔ `{format_currency(int(score))}`")
            
            embed.description = "\n".join(description)
            if interaction.guild and interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.url)
            await interaction.followup.send(embed=embed)
            return
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, (COALESCE(wallet, 0) + COALESCE(bank, 0)) AS total FROM users ORDER BY total DESC")
        rows = cursor.fetchall()

    if not rows:
        return await interaction.followup.send("❌ Aucun classement disponible pour le moment.", ephemeral=True)

    server_total = sum(row[1] for row in rows)
    embed = discord.Embed(title="🏆 Classement", color=0xF1C40F)
    description = [f"🌐 **Global** : `{format_currency(server_total)}`", "──────────────"]
    medals = ["🥇", "🥈", "🥉"]

    for index, (user_id, total) in enumerate(rows[:10], start=1):
        member = interaction.guild.get_member(user_id) if interaction.guild else None
        user_name = member.mention if member else f"<@{user_id}>"
        rank_icon = medals[index - 1] if index <= 3 else f"`#{index:02d}`"
        description.append(f"{rank_icon} {user_name} ➔ `{format_currency(total)}`")

    embed.description = "\n".join(description)
    if interaction.guild and interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="profile", description="Affiche ta carte récapitulative financière, tes statistiques de jeux et ta progression de l'histoire")
async def profile(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user = interaction.user
    wallet, bank, _, streak, beers_today, _, games_played, games_won, games_lost = get_user(user.id)
    total_money = wallet + bank

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM story_progress WHERE user_id = ?", (user.id,))
        res = cursor.fetchone()
        unlocked_episodes_count = res[0] if res else 0

    if unlocked_episodes_count >= 25:
        story_rank = "Légende des Arches 👑"
    elif unlocked_episodes_count >= 15:
        story_rank = "Voyageur Auerguerri 🛡️"
    elif unlocked_episodes_count >= 5:
        story_rank = "Explorateur des Terres 🗺️"
    elif unlocked_episodes_count >= 1:
        story_rank = "Initié des Portes 📜"
    else:
        story_rank = "Étranger égaré 🚶‍♂️"

    embed = discord.Embed(
        title=f"📜 Profil de {user.display_name}",
        color=discord.Color.blurple()
    )
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)

    embed.add_field(name="💰 Finances", value=f"• Portefeuille : **{format_currency(wallet)}**\n• Banque : **{format_currency(bank)}**\n• Total : **{format_currency(total_money)}**", inline=False)
    embed.add_field(name="📖 Progression de l'Histoire", value=f"• Rang : **{story_rank}**\n• Épisodes débloqués : **{unlocked_episodes_count} / 25**", inline=False)
    embed.add_field(name="🍻 Activité & Taverne", value=f"• Série Daily (Streak) : **{streak} jour(s)**\n• Bières bues aujourd'hui : **{beers_today}/5**", inline=False)
    embed.add_field(name="🎲 Statistiques de jeux", value=f"• Parties jouées : **{games_played}**\n• Gagnées : **{games_won}**\n• Perdues : **{games_lost}**", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="story", description="Affiche ta progression et tes épisodes débloqués de Guillaume le Troubadour")
async def story(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user = interaction.user
    file_guillaume = discord.File("assets/guillaume.png", filename="guillaume.png") if os.path.exists("assets/guillaume.png") else None
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT episode_id, unlocked_at FROM story_progress WHERE user_id = ? ORDER BY episode_id ASC", (user.id,))
        rows = cursor.fetchall()

    unlocked_episodes = {row[0]: row[1] for row in rows}
    count = len(unlocked_episodes)

    embed = discord.Embed(
        title=f"📜 Chroniques de Guillaume le Troubadour — {user.display_name}",
        description=f"Épisodes débloqués : **{count} / 25**\n\nUtilise les indices ou participe aux activités du serveur pour découvrir la suite des aventures du Troubadour !",
        color=discord.Color.dark_gold()
    )
    if file_guillaume:
        embed.set_image(url="attachment://guillaume.png")

    if rows:
        latest = rows[-1]
        embed.add_field(name="✨ Dernier épisode découvert", value=f"Épisode #{latest[0]} (débloqué le {latest[1]})", inline=False)
    else:
        embed.add_field(name="✨ Aucun épisode", value="Commence ton aventure pour débloquer ton tout premier épisode !", inline=False)

    kwargs = {"embed": embed, "ephemeral": True}
    if file_guillaume:
        kwargs["file"] = file_guillaume
    await interaction.followup.send(**kwargs)


@bot.tree.command(name="toggle-animations", description="Choisis si tu veux voir le déroulement animé des jeux ou uniquement le résultat final")
@app_commands.choices(mode=[
    app_commands.Choice(name="Activer les animations (Afficher le déroulement)", value="on"),
    app_commands.Choice(name="Désactiver les animations (Cacher le déroulement, afficher uniquement les gains)", value="off")
])
async def toggle_animations(interaction: discord.Interaction, mode: str):
    await interaction.response.defer(ephemeral=True)
    show = (mode == "on")
    set_user_animation_preference(interaction.user.id, show)
    if show:
        embed = discord.Embed(
            title="🎬 Animations Activées",
            description="Le déroulement en direct de vos jeux s'affichera désormais à l'écran.",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="🎬 Animations Masquées",
            description="Le déroulement des jeux restera désormais caché, seuls les gains/résultats finaux s'afficheront.",
            color=discord.Color.orange()
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


def get_user_animation_preference(user_id: int) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT show_animations FROM user_preferences WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT OR IGNORE INTO user_preferences (user_id, show_animations) VALUES (?, 1)", (user_id,))
            conn.commit()
            return True
        return bool(row[0])


def set_user_animation_preference(user_id: int, show: bool):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO user_preferences (user_id, show_animations) VALUES (?, ?)", (user_id, 1 if show else 0))
        conn.commit()


# ==========================================
# 12. INTERFACES DES IA : JIM, JOHN, BROOK & BOB
# ==========================================

class TavernierGamesView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="🎲 Dés", style=discord.ButtonStyle.primary, custom_id="taverne_game_dice")
    async def play_dice(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("🎲 Dés - Mise", run_dice_game))

    @ui.button(label="🎡 Roulette", style=discord.ButtonStyle.primary, custom_id="taverne_game_roulette")
    async def play_roulette(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("🎡 Roulette - Mise", run_roulette_game))

    @ui.button(label="🔫 R. Russe", style=discord.ButtonStyle.danger, custom_id="taverne_game_rr")
    async def play_russian_roulette(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("🔫 Roulette Russe - Mise", run_russian_roulette))

    @ui.button(label="👑 Blackjack", style=discord.ButtonStyle.success, custom_id="taverne_game_bj")
    async def play_bj(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("👑 Blackjack - Mise", run_blackjack_game))

    @ui.button(label="🪙 Slots", style=discord.ButtonStyle.success, custom_id="taverne_game_slots")
    async def play_slots(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("🪙 Slots - Mise", run_slots_game))

    @ui.button(label="✂️ PFC", style=discord.ButtonStyle.secondary, custom_id="taverne_game_pfc")
    async def play_pfc(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("✂️ PFC - Mise", run_pfc_game))

    @ui.button(label="⚜️ Poker", style=discord.ButtonStyle.secondary, custom_id="taverne_game_poker")
    async def play_poker(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("⚜️ Poker Solitaire - Mise", run_poker_game))


class JimTavernView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Commander une Pinte", style=discord.ButtonStyle.primary, emoji="🍺", custom_id="jim_pinte")
    async def pinte(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        retry_after = check_cooldown(user_id, "jim_taverne", 3600)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            msg_text = f'🍺 *Jim te regarde de travers* : "Tu as déjà bu, attends **{minutes}m {seconds}s**."'
            return await interaction.followup.send(msg_text, ephemeral=True)

        wallet, _, _, _, beers_today, last_beer_date, _, _, _ = get_user(user_id)

        today_str = time.strftime("%Y-%m-%d")
        if last_beer_date != today_str:
            beers_today = 0
            last_beer_date = today_str

        if beers_today >= 5 and not TEST_MODE_ENABLED:
            return await interaction.followup.send("🍺 *Jim croise les bras et repousse ta chope* : \"Non, mon ami, ça suffit pour aujourd'hui ! Tu es déjà bien trop saoul, reviens demain.\"", ephemeral=True)

        if wallet < 50:
            return await interaction.followup.send("🍺 *Jim* : \"Tu n'as même pas 50 $ pour payer ta pinte !\"", ephemeral=True)

        update_wallet(user_id, -50)
        beers_today += 1
        update_quest_progress(user_id, "beer_drunk", 1)
        update_quest_progress_v2(user_id, "beer_drunk", 1)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET beers_today = ?, last_beer_date = ? WHERE user_id = ?",
                (beers_today, last_beer_date, user_id)
            )
            conn.commit()

        if REDIS_AVAILABLE and redis_client:
            redis_client.delete(f"user:{user_id}")

        await check_and_unlock_achievements(user_id, bot_client=bot)

        events = [
            ("gain", 200, f"🍻 Tu as passé une excellente soirée et gagné à un jeu de dés clandestin ! +**{format_currency(200)}**"),
            ("gain", 100, f"🍻 Tu as bu un coup avec des marchands, ils t'ont offert des babioles revendues. +**{format_currency(100)}**"),
            ("loss", 50, f"💤 Tu t'es endormi sur une table... Quelqu'un t'a fait les poches ! -**{format_currency(50)}**"),
            ("loss", 80, f"💥 En te levant d'un coup un peu trop sec, tu bouscules un client et dois payer pour casser sa chope ! -**{format_currency(80)}**"),
            ("neutral", 0, f"🍖 Jim t'a servi une pinte bien fraîche et un ragoût maison. Santé !"),
        ]

        event_type, amount, outcome = random.choice(events)
        if event_type == "gain":
            update_wallet(user_id, amount)
        elif event_type == "loss":
            current_wallet, _, _, _, _, _, _, _, _ = get_user(user_id)
            actual_loss = min(amount, current_wallet)
            if actual_loss > 0:
                update_wallet(user_id, -actual_loss)

        await send_public_log(
            content=f"🍺 **{interaction.user.display_name}** a commandé une pinte chez Jim ! (#{beers_today}/5) {outcome}"
        )

        await interaction.followup.send(f"🪵 **[JIM LE TAVERNIER]** (Pinte #{beers_today}/5) {outcome}", ephemeral=True)

    @ui.button(label="Jeux de la Taverne", style=discord.ButtonStyle.success, emoji="🎲", custom_id="jim_games")
    async def games_hub(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="🎲 Coin des Jeux de la Taverne",
            description="Choisis un jeu ci-dessous pour lancer une partie avec une mise :",
            color=discord.Color.dark_orange()
        )
        await interaction.response.send_message(embed=embed, view=TavernierGamesView(), ephemeral=True)

    @ui.button(label="Défier un joueur (Duel)", style=discord.ButtonStyle.danger, emoji="⚔️", custom_id="jim_duel")
    async def duel_hub(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="⚔️ Coin des Duels de la Taverne",
            description="Choisis le type de jeu pour ton duel face à un autre habitué :",
            color=discord.Color.dark_red()
        )
        await interaction.response.send_message(embed=embed, view=TavernDuelChoiceView(), ephemeral=True)


class TavernDuelChoiceView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="Dés du Destin", style=discord.ButtonStyle.primary, emoji="🎲", custom_id="jim_duel_choice_dice")
    async def choice_dice(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="⚔️ Duel de Dés - Choix de l'adversaire",
            description="Sélectionne le joueur à affronter dans le menu déroulant ci-dessous :",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, view=TavernDuelSelectView("dice"), ephemeral=True)

    @ui.button(label="Pierre-Feuille-Ciseaux", style=discord.ButtonStyle.secondary, emoji="✂️", custom_id="jim_duel_choice_pfc")
    async def choice_pfc(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="⚔️ Duel PFC - Choix de l'adversaire",
            description="Sélectionne le joueur à affronter dans le menu déroulant ci-dessous :",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, view=TavernDuelSelectView("pfc"), ephemeral=True)


class TavernDuelSelectView(ui.View):
    def __init__(self, game_type: str):
        super().__init__(timeout=60)
        self.add_item(TavernDuelSelect(game_type))


class TavernDuelSelect(ui.UserSelect):
    def __init__(self, game_type: str):
        super().__init__(
            placeholder="Choisis ton adversaire pour le duel...",
            min_values=1,
            max_values=1,
            custom_id=f"jim_duel_select_{game_type}"
        )
        self.game_type = game_type

    async def callback(self, interaction: discord.Interaction):
        opponent = self.values[0]
        if opponent.id == interaction.user.id:
            return await interaction.response.send_message("❌ Tu ne peux pas te défier toi-même !", ephemeral=True)
        if opponent.bot:
            return await interaction.response.send_message("❌ Tu ne peux pas défier un bot !", ephemeral=True)

        await interaction.response.send_modal(TavernDuelBetModal(opponent, self.game_type))


class TavernDuelBetModal(ui.Modal, title="⚔️ Tavernier - Configuration du Duel"):
    bet_input = ui.TextInput(label="Montant de la mise", placeholder="Ex: 100", required=True, max_length=6)

    def __init__(self, opponent: discord.Member, game_type: str):
        super().__init__()
        self.opponent = opponent
        self.game_type = game_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        try:
            bet = int(self.bet_input.value)
        except ValueError:
            return await interaction.followup.send("❌ Veuillez entrer un nombre entier valide.", ephemeral=True)

        if self.opponent.id == interaction.user.id:
            return await interaction.followup.send("❌ Vous ne pouvez pas vous affronter vous-même !", ephemeral=True)
        if self.opponent.bot:
            return await interaction.followup.send("❌ Vous ne pouvez pas affronter un bot !", ephemeral=True)
        if bet <= 0:
            return await interaction.followup.send("❌ La mise doit être supérieure à 0 $ !", ephemeral=True)
        if bet > MAX_BET:
            return await interaction.followup.send(f"❌ La mise maximale autorisée est de **{MAX_BET} $** !", ephemeral=True)

        wallet_chal, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
        if wallet_chal < bet:
            return await interaction.followup.send("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)

        wallet_opp, _, _, _, _, _, _, _, _ = get_user(self.opponent.id)
        if wallet_opp < bet:
            return await interaction.followup.send(f"❌ {self.opponent.mention} n'a pas assez d'argent dans son portefeuille pour accepter cette mise.", ephemeral=True)

        view = DuelAcceptView(interaction.user, self.opponent, self.game_type, bet, from_jim=True, interaction_ref=interaction)
        
        embed = discord.Embed(
            title="⚔️ DÉFI DE DUEL (TAVERNE)",
            description=(
                f"{interaction.user.mention} défie {self.opponent.mention} à un duel de **{self.game_type}** sous l'œil de Jim !\n\n"
                f"💰 **Mise en jeu** : `{format_currency(bet)}` par joueur\n\n"
                f"{self.opponent.mention}, acceptez-vous ce défi ?\n\n"
                f"⏰ Vous avez **30 secondes** pour répondre !"
            ),
            color=discord.Color.dark_red()
        )
        view.public_message = await send_public_log(embed=embed, view=view)
        if view.public_message is None:
            return await interaction.followup.send("❌ Impossible d'envoyer la demande de duel dans le Salon B. Vérifie les permissions du bot.", ephemeral=True)
        await interaction.followup.send("✅ Demande de duel envoyée dans le Salon B.", ephemeral=True)


class DuelAcceptView(ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, game_type: str, bet: int, from_jim: bool = True, interaction_ref: discord.Interaction = None):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.opponent = opponent
        self.game_type = game_type
        self.bet = bet
        self.from_jim = from_jim
        self.interaction_ref = interaction_ref
        self.public_message = None
        self.responded = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Seul l'adversaire défié peut accepter ou refuser ce duel.", ephemeral=True)
            return False
        if self.responded:
            await interaction.response.send_message("❌ Ce duel a déjà reçu une réponse.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.responded:
            return
        
        for child in self.children:
            child.disabled = True
        
        mock_message = random.choice([
            "😱 **{opponent}** a eu la trouille et s'est caché !",
            "🫣 **{opponent}** a préféré sauver sa peau !",
            "🐔 **{opponent}** a picoré et s'est envolé !",
            "🏃‍♂️ **{opponent}** a pris ses jambes à son cou !"
        ]).format(opponent=self.opponent.mention, challenger=self.challenger.mention)
        
        location = "la taverne" if self.from_jim else "l'arène"
        embed = discord.Embed(
            title="⏰ DUEL EXPIRÉ",
            description=f"{mock_message}\n\nLe duel à {location} a été automatiquement annulé.",
            color=discord.Color.dark_red()
        )
        
        try:
            if self.public_message:
                await self.public_message.edit(embed=embed, view=self)
            elif self.interaction_ref:
                try:
                    await self.interaction_ref.edit_original_response(embed=embed, view=self)
                except Exception:
                    pass
            
            await send_public_log(
                content=f"⏰ **{self.challenger.display_name}** a défié {self.opponent.display_name} mais celui-ci n'a pas répondu à temps ! (30s) 🐔"
            )
        except Exception as e:
            print(f"❌ Erreur expiration duel : {e}")

    @ui.button(label="Accepter le Duel", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        wallet_opp, _, _, _, _, _, _, _, _ = get_user(self.opponent.id)
        wallet_chal, _, _, _, _, _, _, _, _ = get_user(self.challenger.id)

        if wallet_opp < self.bet:
            return await interaction.response.send_message("❌ Vous n'avez pas assez d'argent dans votre portefeuille pour accepter ce duel.", ephemeral=True)
        if wallet_chal < self.bet:
            return await interaction.response.send_message(f"❌ {self.challenger.mention} n'a plus assez d'argent pour honorer le duel.", ephemeral=True)

        self.responded = True
        for child in self.children:
            child.disabled = True

        update_quest_progress(self.challenger.id, "duel_played", 1)
        update_quest_progress(self.opponent.id, "duel_played", 1)

        location = "de la taverne" if self.from_jim else "de l'arène"
        await send_public_log(
            content=f"⚔️ **{self.challenger.display_name}** et **{self.opponent.display_name}** s'affrontent dans un duel {location} ! Mise : **{format_currency(self.bet)}**"
        )

        if self.game_type == "pfc":
            view = DuelPFCView(self.challenger, self.opponent, self.bet)
            embed = discord.Embed(
                title="⚔️ DUEL PFC EN COURS",
                description=f"Affrontement entre {self.challenger.mention} et {self.opponent.mention} !\nMise en jeu : **{format_currency(self.bet)}**\n\nChacun doit faire son choix en privé via les boutons ci-dessous :",
                color=discord.Color.orange()
            )
            await interaction.response.edit_message(embed=embed, view=view)
        elif self.game_type == "dice":
            view = DuelDiceView(self.challenger, self.opponent, self.bet)
            embed = discord.Embed(
                title="⚔️ DUEL DÉS EN COURS",
                description=f"Affrontement entre {self.challenger.mention} et {self.opponent.mention} !\nMise en jeu : **{format_currency(self.bet)}**\n\nCliquez sur le bouton pour lancer vos dés :",
                color=discord.Color.orange()
            )
            await interaction.response.edit_message(embed=embed, view=view)

    @ui.button(label="Refuser", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        self.responded = True
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(
            title="⚔️ Duel Refusé",
            description=f"{self.opponent.mention} a décliné le duel proposé par {self.challenger.mention}.",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await send_public_log(
            content=f"❌ **{self.opponent.display_name}** a refusé le duel de **{self.challenger.display_name}** !"
        )


class DuelPFCView(ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet
        self.choices = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in [self.challenger.id, self.opponent.id]:
            await interaction.response.send_message("❌ Ce duel ne vous concerne pas !", ephemeral=True)
            return False
        if interaction.user.id in self.choices:
            await interaction.response.send_message("❌ Vous avez déjà fait votre choix !", ephemeral=True)
            return False
        return True

    async def process_choice(self, interaction: discord.Interaction, choice: str):
        self.choices[interaction.user.id] = choice
        await interaction.response.send_message(f"🔒 Choix enregistré : **{choice}**.", ephemeral=True)

        if len(self.choices) == 2:
            for child in self.children:
                child.disabled = True

            c_choice = self.choices[self.challenger.id]
            o_choice = self.choices[self.opponent.id]

            emaps = {"pierre": "🪨 Pierre", "feuille": "📄 Feuille", "ciseau": "✂️ Ciseau"}

            if c_choice == o_choice:
                res_text = "🤝 **Égalité !** Personne ne remporte la mise."
            elif ((c_choice == "pierre" and o_choice == "ciseau") or
                  (c_choice == "feuille" and o_choice == "pierre") or
                  (c_choice == "ciseau" and o_choice == "feuille")):
                update_wallet(self.challenger.id, self.bet)
                update_wallet(self.opponent.id, -self.bet)
                update_game_stats(self.challenger.id, won=True)
                update_game_stats(self.opponent.id, won=False)
                await check_and_unlock_achievements(self.challenger.id, bot_client=bot)
                res_text = f"🏆 **Victoire de {self.challenger.mention} !** Il remporte **{format_currency(self.bet)}**."
                await send_public_log(
                    content=f"⚔️ **{self.challenger.display_name}** a remporté un duel PFC contre {self.opponent.display_name} ! +**{format_currency(self.bet)}**"
                )
            else:
                update_wallet(self.opponent.id, self.bet)
                update_wallet(self.challenger.id, -self.bet)
                update_game_stats(self.opponent.id, won=True)
                update_game_stats(self.challenger.id, won=False)
                await check_and_unlock_achievements(self.opponent.id, bot_client=bot)
                res_text = f"🏆 **Victoire de {self.opponent.mention} !** Il remporte **{format_currency(self.bet)}**."
                await send_public_log(
                    content=f"⚔️ **{self.opponent.display_name}** a remporté un duel PFC contre {self.challenger.display_name} ! +**{format_currency(self.bet)}**"
                )

            embed = discord.Embed(
                title="⚔️ RÉSULTAT DU DUEL PFC",
                description=(
                    f"👤 {self.challenger.mention} a choisi : `{emaps[c_choice]}`\n"
                    f"👤 {self.opponent.mention} a choisi : `{emaps[o_choice]}`\n\n"
                    f"{res_text}"
                ),
                color=discord.Color.gold()
            )
            await interaction.message.edit(embed=embed, view=self)

    @ui.button(label="Pierre", style=discord.ButtonStyle.primary, emoji="🪨")
    async def fn_pierre(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_choice(interaction, "pierre")

    @ui.button(label="Feuille", style=discord.ButtonStyle.success, emoji="📄")
    async def fn_feuille(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_choice(interaction, "feuille")

    @ui.button(label="Ciseau", style=discord.ButtonStyle.danger, emoji="✂️")
    async def fn_ciseau(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_choice(interaction, "ciseau")


class DuelDiceView(ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet
        self.rolls = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in [self.challenger.id, self.opponent.id]:
            await interaction.response.send_message("❌ Ce duel ne vous concerne pas !", ephemeral=True)
            return False
        if interaction.user.id in self.rolls:
            await interaction.response.send_message("❌ Vous avez déjà lancé vos dés !", ephemeral=True)
            return False
        return True

    @ui.button(label="🎲 Lancer les dés", style=discord.ButtonStyle.primary)
    async def roll_btn(self, interaction: discord.Interaction, button: ui.Button):
        d1, d2 = random.randint(1, 6), random.randint(1, 6)
        total = d1 + d2
        self.rolls[interaction.user.id] = total
        await interaction.response.send_message(f"🎲 Vous avez obtenu **{total}** ({d1} + {d2}).", ephemeral=True)

        if len(self.rolls) == 2:
            for child in self.children:
                child.disabled = True

            c_score = self.rolls[self.challenger.id]
            o_score = self.rolls[self.opponent.id]

            if c_score == o_score:
                res_text = "🤝 **Égalité parfaite !** Les mises sont remboursées."
            elif c_score > o_score:
                update_wallet(self.challenger.id, self.bet)
                update_wallet(self.opponent.id, -self.bet)
                update_game_stats(self.challenger.id, won=True)
                update_game_stats(self.opponent.id, won=False)
                await check_and_unlock_achievements(self.challenger.id, bot_client=bot)
                res_text = f"🏆 **Victoire de {self.challenger.mention} ({c_score} vs {o_score}) !** Il remporte **{format_currency(self.bet)}**."
                await send_public_log(
                    content=f"🎲 **{self.challenger.display_name}** a remporté un duel de dés contre {self.opponent.display_name} ! +**{format_currency(self.bet)}**"
                )
            else:
                update_wallet(self.opponent.id, self.bet)
                update_wallet(self.challenger.id, -self.bet)
                update_game_stats(self.opponent.id, won=True)
                update_game_stats(self.challenger.id, won=False)
                await check_and_unlock_achievements(self.opponent.id, bot_client=bot)
                res_text = f"🏆 **Victoire de {self.opponent.mention} ({o_score} vs {c_score}) !** Il remporte **{format_currency(self.bet)}**."
                await send_public_log(
                    content=f"🎲 **{self.opponent.display_name}** a remporté un duel de dés contre {self.challenger.display_name} ! +**{format_currency(self.bet)}**"
                )

            embed = discord.Embed(
                title="⚔️ RÉSULTAT DU DUEL DÉS",
                description=(
                    f"👤 {self.challenger.mention} : `{c_score}`\n"
                    f"👤 {self.opponent.mention} : `{o_score}`\n\n"
                    f"{res_text}"
                ),
                color=discord.Color.gold()
            )
            await interaction.message.edit(embed=embed, view=self)


# ==========================================
# JOHN - BRIGAND
# ==========================================

class JohnCrimeView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Tenter un Crime", style=discord.ButtonStyle.danger, emoji="🥷", custom_id="john_crime_btn")
    async def crime_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        retry_after = check_cooldown(user_id, "john_crime", 60)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            msg_text = f'🥷 *John* : "Reviens dans **{minutes}m {seconds}s**."'
            return await interaction.followup.send(msg_text, ephemeral=True)

        success = random.choice([True, False])
        wallet, _, _, _, _, _, _, _, _ = get_user(user_id)
        update_quest_progress(user_id, "crime_attempt", 1)
        update_quest_progress_v2(user_id, "crime_attempt", 1)

        if success:
            gain = random.randint(300, 1000)
            update_wallet(user_id, gain)
            await check_and_unlock_achievements(user_id, bot_client=bot)
            
            await send_public_log(
                content=f"🥷 **{interaction.user.display_name}** a réussi un crime et a gagné **{format_currency(gain)}** !"
            )
            
            await interaction.followup.send(f"🥷 **[JOHN LE BRIGAND]** Joli coup ! Vol réussi. +**{format_currency(gain)}**", ephemeral=True)
        else:
            loss = min(random.randint(100, 400), wallet)
            if loss > 0:
                update_wallet(user_id, -loss)
                await send_public_log(
                    content=f"🚨 **{interaction.user.display_name}** s'est fait prendre par la milice lors d'un crime ! Amende : **{format_currency(loss)}**"
                )
                await interaction.followup.send(f"🚨 **[JOHN LE BRIGAND]** La milice t'a repéré ! Amende : -**{format_currency(loss)}**", ephemeral=True)
            else:
                await interaction.followup.send("🚨 **[JOHN LE BRIGAND]** Pris la main dans le sac, mais tu es trop pauvre pour payer.", ephemeral=True)

    @ui.button(label="Braquage de la Brinks", style=discord.ButtonStyle.success, emoji="🔐", custom_id="john_vault_btn")
    async def vault_btn(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        retry_after = check_cooldown(user_id, "john_vault", 3600)
        if retry_after > 0:
            hours, remainder = divmod(retry_after, 3600)
            minutes, seconds = divmod(remainder, 60)
            return await interaction.response.send_message(f'🔐 *John* : "Attends **{hours}h {minutes}m {seconds}s**."', ephemeral=True)

        update_quest_progress(user_id, "vault_attempt", 1)
        update_quest_progress_v2(user_id, "vault_attempt", 1)
        prize = random.randint(2000, 7500)
        secret_code = f"{random.randint(0, 9)}{random.randint(0, 9)}{random.randint(0, 9)}{random.randint(0, 9)}"

        embed = discord.Embed(
            title="🔐 Braquage de la Brinks",
            description=(
                "*(John t'amène devant un lourd coffre-fort blindé)*\n\n"
                f"Le coffre contient **{format_currency(prize)}** !\n"
                "Tu disposes de **5 tentatives** pour deviner le code.\n"
                "Attention : si tu échoues, la police te prélève 5% de ton compte bancaire !"
            ),
            color=discord.Color.dark_purple()
        )
        await interaction.response.send_message(embed=embed, view=BrinksVaultView(prize, 5, secret_code), ephemeral=True)


class BrinksVaultView(ui.View):
    def __init__(self, prize: int, attempts_left: int, secret_code: str):
        super().__init__(timeout=60)
        self.prize = prize
        self.attempts_left = attempts_left
        self.secret_code = secret_code

    @ui.button(label="Entrer une combinaison", style=discord.ButtonStyle.danger, emoji="🔢", custom_id="brinks_vault_input")
    async def try_code_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrinksVaultModal(self.prize, self.attempts_left, self.secret_code))


class BrinksVaultModal(ui.Modal, title="🔒 Coffre de la Brinks - Code à 4 chiffres"):
    code_input = ui.TextInput(label="Entrer la combinaison (4 chiffres)", placeholder="Ex: 4812", required=True, min_length=4, max_length=4)

    def __init__(self, prize: int, attempts_left: int, secret_code: str):
        super().__init__()
        self.prize = prize
        self.attempts_left = attempts_left
        self.secret_code = secret_code

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_input = self.code_input.value
        if not user_input.isdigit() or len(user_input) != 4:
            return await interaction.followup.send("❌ Le code doit être 4 chiffres !", ephemeral=True)

        user_id = interaction.user.id

        if user_input == self.secret_code:
            update_wallet(user_id, self.prize)
            await check_and_unlock_achievements(user_id, bot_client=bot)
            
            await send_public_log(
                content=f"🔐 **{interaction.user.display_name}** a réussi le braquage de la Brinks ! +**{format_currency(self.prize)}**"
            )
            
            embed = discord.Embed(
                title="🔐 [BRINKS] COFFRE OUVERT !",
                description=f"🎉 Tu as trouvé la bonne combinaison **{self.secret_code}** !\nTu récupères **{format_currency(self.prize)}** !",
                color=discord.Color.green()
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        hints = []
        for i in range(4):
            if user_input[i] == self.secret_code[i]:
                hints.append(f"Chiffre {i+1} : **Bien placé**")
            elif user_input[i] in self.secret_code:
                hints.append(f"Chiffre {i+1} : **Bon mais mauvais endroit**")
            else:
                hints.append(f"Chiffre {i+1} : **Incorrect**")

        self.attempts_left -= 1

        if self.attempts_left > 0:
            view = BrinksVaultView(self.prize, self.attempts_left, self.secret_code)
            embed = discord.Embed(
                title="🔒 [BRINKS] Alarme retentit...",
                description=(
                    f"❌ Mauvaise combinaison !\n"
                    f"Tentatives restantes : **{self.attempts_left}/5**\n\n"
                    "**Indices :**\n" + "\n".join(hints)
                ),
                color=discord.Color.orange()
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            _, bank, _, _, _, _, _, _, _ = get_user(user_id)
            fine = int(bank * 0.05)
            if fine > 0:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE users SET bank = bank - ? WHERE user_id = ?", (fine, user_id))
                    conn.commit()

            await send_public_log(
                content=f"🚨 **{interaction.user.display_name}** s'est fait prendre lors d'un braquage ! Amende : **-{format_currency(fine)}**"
            )

            embed = discord.Embed(
                title="🚨 [BRINKS] ARRIVÉE DE LA POLICE !",
                description=f"💥 Trop de temps perdu ! Les forces de l'ordre débarquent.\nTu t'enfuis mais la police te saisit **-{format_currency(fine)}** !",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


# ==========================================
# BOB LE MAITRE D'ARME
# ==========================================

class BobArenaView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Entrer dans l'Arène (Combattre Bob)", style=discord.ButtonStyle.danger, emoji="⚔️", custom_id="bob_arena_fight")
    async def fight_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("⚔️ Arène - Mise de Combat", run_arena_fight))


async def run_arena_fight(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "arene_fight", bet, cooldown_sec=1800):
        return

    update_quest_progress(interaction.user.id, "arena_fight", 1)
    update_quest_progress_v2(interaction.user.id, "arena_fight", 1)
    
    await send_public_log(
        content=f"⚔️ **{interaction.user.display_name}** entre dans l'arène pour affronter Bob ! Mise : **{format_currency(bet)}**"
    )
    
    view = ArenaFightView(interaction.user.id, bet)
    embed = view.build_embed("Le combat commence !", color=discord.Color.orange())
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class ArenaFightView(ui.View):
    def __init__(self, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.bet = bet
        self.player_hp = 100
        self.bob_hp = 100
        self.round_count = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ce n'est pas votre combat !", ephemeral=True)
            return False
        return True

    def build_embed(self, status_msg: str, color=discord.Color.red()) -> discord.Embed:
        desc = (
            f"⚔️ **DUEL DANS L'ARÈNE** ⚔️\n\n"
            f"👤 **Votre PV** : `{'❤️' * max(1, int(self.player_hp / 10))}` ({self.player_hp}/100)\n"
            f"🛡️ **Bob** : `{'🖤' * max(1, int(self.bob_hp / 10))}` ({self.bob_hp}/100)\n\n"
            f"📜 {status_msg}"
        )
        embed = discord.Embed(title="🏟️ Arène des IV Sceaux", description=desc, color=color)
        embed.set_footer(text=f"Mise : {format_currency(self.bet)}")
        return embed

    async def process_turn(self, interaction: discord.Interaction, player_move: str):
        self.round_count += 1
        
        if player_move == "heavy":
            p_dmg = random.randint(18, 32) if random.random() < 0.6 else 0
            p_text = f"Frappe lourde : **{p_dmg} dégâts** !" if p_dmg > 0 else "Frappe lourde dans le vide !"
        elif player_move == "fast":
            p_dmg = random.randint(10, 18)
            p_text = f"Estoc rapide : **{p_dmg} dégâts** !"
        else:
            p_dmg = 0
            p_text = "Posture défensive."

        self.bob_hp = max(0, self.bob_hp - p_dmg)

        if self.bob_hp <= 0:
            for child in self.children:
                child.disabled = True
            gain = self.bet * 2
            update_wallet(self.user_id, gain - self.bet)
            update_game_stats(self.user_id, won=True)
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            
            await send_public_log(
                content=f"⚔️ **{interaction.user.display_name}** a vaincu Bob ! +**{format_currency(gain)}**"
            )
            
            embed = self.build_embed(f"{p_text}\n\n🏆 **VICTOIRE !** +**{format_currency(gain)}**", color=discord.Color.green())
            await interaction.response.edit_message(embed=embed, view=self)
            return

        bob_move = random.choice(["heavy", "fast", "bash"])
        if bob_move == "heavy" and player_move != "parry":
            b_dmg = random.randint(15, 25)
            b_text = f"Massue de Bob : **-{b_dmg} PV** !"
        elif bob_move == "fast":
            b_dmg = random.randint(8, 15)
            b_text = f"Dague de Bob : **-{b_dmg} PV** !"
        else:
            b_dmg = 5 if player_move != "parry" else 0
            b_text = f"Bouclier de Bob : **-{b_dmg} PV** !" if b_dmg > 0 else "Garde parfaite !"

        self.player_hp = max(0, self.player_hp - b_dmg)

        if self.player_hp <= 0:
            for child in self.children:
                child.disabled = True
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            
            await send_public_log(
                content=f"💀 **{interaction.user.display_name}** a été vaincu par Bob ! -**{format_currency(self.bet)}**"
            )
            
            embed = self.build_embed(f"{p_text}\n{b_text}\n\n💀 **DÉFAITE !** -**{format_currency(self.bet)}**", color=discord.Color.dark_red())
            await interaction.response.edit_message(embed=embed, view=self)
            return

        embed = self.build_embed(f"{p_text}\n{b_text}")
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="🪓 Frappe Lourde (60%)", style=discord.ButtonStyle.danger, custom_id="arena_heavy")
    async def heavy_strike(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_turn(interaction, "heavy")

    @ui.button(label="🗡️ Estoc Rapide (100%)", style=discord.ButtonStyle.primary, custom_id="arena_fast")
    async def fast_strike(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_turn(interaction, "fast")

    @ui.button(label="🛡️ Posture Défensive", style=discord.ButtonStyle.secondary, custom_id="arena_parry")
    async def parry_stance(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_turn(interaction, "parry")


# ==========================================
# PMU ET BROOK
# ==========================================

PMU_ODDS = {1: 3.0, 2: 3.0, 3: 3.0, 4: 3.0}


async def run_pmu_game(interaction: discord.Interaction, cheval: int, bet: int):
    if not await validate_game_bet(interaction, "pmu", bet, cooldown_sec=900):
        return

    update_quest_progress(interaction.user.id, "pmu_bet", 1)
    update_quest_progress_v2(interaction.user.id, "pmu_bet", 1)
    show_anim = get_user_animation_preference(interaction.user.id)

    chevaux = {
        1: {"nom": "Canabis", "emoji": "🐎"},
        2: {"nom": "Jolly Jumper", "emoji": "🐴"},
        3: {"nom": "Pégase", "emoji": "🦄"},
        4: {"nom": "Petit Tonnerre", "emoji": "🏇"},
    }

    piste_len = 10
    positions = {1: 0, 2: 0, 3: 0, 4: 0}

    initial_piste = "🏁 **PMU - Départ !**\n```text\n┌── HIPPODROME ────────┐\n"
    for cid, data in chevaux.items():
        initial_piste += f"│#{cid}[{data['emoji']}{'-'*piste_len}]│\n"
    initial_piste += "└──────────────────────┘\n```"

    await interaction.followup.send(initial_piste, ephemeral=True)
    anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

    while max(positions.values()) < piste_len:
        await asyncio.sleep(1.0)
        for c in positions:
            if positions[c] < piste_len:
                positions[c] += random.randint(1, 3)
                if positions[c] > piste_len:
                    positions[c] = piste_len

        piste_str = "🏁 **PMU - Course...**\n```text\n┌── HIPPODROME ────────┐\n"
        for cid, data in chevaux.items():
            p = positions[cid]
            ligne = "-" * p + data["emoji"] + "-" * (piste_len - p)
            piste_str += f"│#{cid}[{ligne}]│\n"
        piste_str += "└──────────────────────┘\n```"
        await anim_manager.update_animation(new_content=piste_str)

    max_p = max(positions.values())
    gagnants = [c for c, p in positions.items() if p >= max_p]
    gagnant = random.choice(gagnants)

    cote = PMU_ODDS[cheval]

    if cheval == gagnant:
        gain = int(bet * cote)
        update_wallet(interaction.user.id, gain - bet)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        res_msg = f"🏆 **[PMU] VICTOIRE !** #{gagnant} ({chevaux[gagnant]['nom']}) a gagné ! +**{format_currency(gain)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a gagné au PMU sur {chevaux[cheval]['nom']} ! +**{format_currency(gain)}**"
        )
    else:
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        res_msg = f"❌ **[PMU] PERDU !** #{gagnant} ({chevaux[gagnant]['nom']}) a gagné. -**{format_currency(bet)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a perdu au PMU ! -**{format_currency(bet)}**"
        )

    final_piste = "🏁 **PMU - Arrivée !**\n```text\n┌── HIPPODROME ────────┐\n"
    for cid, data in chevaux.items():
        p = positions[cid]
        ligne = "-" * p + data["emoji"] + "-" * (piste_len - p)
        final_piste += f"│#{cid}[{ligne}]│\n"
    final_piste += f"└──────────────────────┘\n```\n{res_msg}"

    if not show_anim:
        try:
            await interaction.edit_original_response(content=final_piste)
        except discord.HTTPException:
            pass
    else:
        await anim_manager.update_animation(new_content=final_piste)


def generate_brook_odds():
    odds = {
        1: round(random.uniform(1.3, 5.5), 2),
        2: round(random.uniform(1.3, 5.5), 2),
        3: round(random.uniform(1.3, 5.5), 2),
        4: round(random.uniform(1.3, 5.5), 2)
    }
    return odds


class BrookBookmakerView(ui.View):
    def __init__(self, odds: dict):
        super().__init__(timeout=None)
        self.odds = odds

        self.horse_1.label = f"Canabis (x{odds[1]})"
        self.horse_2.label = f"Jolly Jumper (x{odds[2]})"
        self.horse_3.label = f"Pégase (x{odds[3]})"
        self.horse_4.label = f"Petit Tonnerre (x{odds[4]})"

    @ui.button(label="Canabis", style=discord.ButtonStyle.primary, emoji="🐎", custom_id="brook_horse_1")
    async def horse_1(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrookPMUBetModal(1, self.odds, panel_message=interaction.message))

    @ui.button(label="Jolly Jumper", style=discord.ButtonStyle.primary, emoji="🐴", custom_id="brook_horse_2")
    async def horse_2(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrookPMUBetModal(2, self.odds, panel_message=interaction.message))

    @ui.button(label="Pégase", style=discord.ButtonStyle.primary, emoji="🦄", custom_id="brook_horse_3")
    async def horse_3(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrookPMUBetModal(3, self.odds, panel_message=interaction.message))

    @ui.button(label="Petit Tonnerre", style=discord.ButtonStyle.primary, emoji="🏇", custom_id="brook_horse_4")
    async def horse_4(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrookPMUBetModal(4, self.odds, panel_message=interaction.message))


async def run_brook_pmu_game(interaction: discord.Interaction, horse_choice: int, bet: int, dynamic_odds: dict, panel_message=None):
    if not await validate_game_bet(interaction, "brook_bet", bet, cooldown_sec=1800):
        return

    update_quest_progress(interaction.user.id, "pmu_bet", 1)
    update_quest_progress_v2(interaction.user.id, "pmu_bet", 1)
    show_anim = get_user_animation_preference(interaction.user.id)

    chevaux = {
        1: {"nom": "Canabis", "emoji": "🐎"},
        2: {"nom": "Jolly Jumper", "emoji": "🐴"},
        3: {"nom": "Pégase", "emoji": "🦄"},
        4: {"nom": "Petit Tonnerre", "emoji": "🏇"},
    }

    piste_len = 10
    positions = {1: 0, 2: 0, 3: 0, 4: 0}

    initial_piste = "🏁 **Brook - Départ !**\n```text\n┌── HIPPODROME ────────┐\n"
    for cid, data in chevaux.items():
        initial_piste += f"│#{cid}[{data['emoji']}{'-'*piste_len}]│\n"
    initial_piste += "└──────────────────────┘\n```"

    course_message = await interaction.followup.send(initial_piste, ephemeral=True, wait=True)

    async def edit_course(content: str):
        try:
            await course_message.edit(content=content)
        except:
            pass

    weights = [round(10 / dynamic_odds[i], 2) for i in range(1, 5)]

    while max(positions.values()) < piste_len:
        await asyncio.sleep(1.0)
        for c in positions:
            if positions[c] < piste_len:
                positions[c] += random.randint(1, 3)
                if positions[c] > piste_len:
                    positions[c] = piste_len

        piste_str = "🏁 **Brook - Course...**\n```text\n┌── HIPPODROME ────────┐\n"
        for cid, data in chevaux.items():
            p = positions[cid]
            ligne = "-" * p + data["emoji"] + "-" * (piste_len - p)
            piste_str += f"│#{cid}[{ligne}]│\n"
        piste_str += "└──────────────────────┘\n```"
        await edit_course(piste_str)

    max_p = max(positions.values())
    gagnants = [c for c, p in positions.items() if p >= max_p]
    gagnant = random.choices(gagnants, weights=[weights[c-1] for c in gagnants], k=1)[0] if len(gagnants) > 1 else gagnants[0]

    cote = dynamic_odds[horse_choice]

    if horse_choice == gagnant:
        gain = int(bet * cote)
        update_wallet(interaction.user.id, gain - bet)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        res_msg = f"🏆 **[BROOK] VICTOIRE !** #{gagnant} ({chevaux[gagnant]['nom']}) ! +**{format_currency(gain)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a gagné chez Brook sur {chevaux[horse_choice]['nom']} ! +**{format_currency(gain)}**"
        )
    else:
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        res_msg = f"❌ **[BROOK] PERDU !** #{gagnant} ({chevaux[gagnant]['nom']}) a gagné. -**{format_currency(bet)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a perdu chez Brook ! -**{format_currency(bet)}**"
        )

    final_piste = f"🏁 **Brook - Arrivée !**\n```text\n┌── HIPPODROME ────────┐\n"
    for cid, data in chevaux.items():
        p = positions[cid]
        ligne = "-" * p + data["emoji"] + "-" * (piste_len - p)
        final_piste += f"│#{cid}[{ligne}]│\n"
    final_piste += f"└──────────────────────┘\n```\n{res_msg}"

    await edit_course(final_piste)

    # Mettre à jour le panneau Brook
    new_odds = generate_brook_odds()
    new_embed = discord.Embed(
        description=(
            "📜 **Guichet des Paris — BROOK**\n"
            f"*(Tient un carnet de notes)* Les cotes ont varié ! "
            f"Canabis (x{new_odds[1]}), Jolly Jumper (x{new_odds[2]}), Pégase (x{new_odds[3]}), Petit Tonnerre (x{new_odds[4]})."
        ),
        color=0x1ABC9C
    )

    try:
        if panel_message is not None:
            await panel_message.edit(embed=new_embed, view=BrookBookmakerView(new_odds))
    except Exception as e:
        print(f"❌ Erreur mise à jour Brook : {e}")


# ==========================================
# 13. COMMANDES DE JEUX (Raccourcis)
# ==========================================

async def run_dice_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "dice", bet):
        return
    
    show_anim = get_user_animation_preference(interaction.user.id)
    mini_dice = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
    name = interaction.user.display_name.upper()[:10]

    def get_dice_box(d1, d2, p1, p2, status):
        ds = d1 + d2 if d1 else 0
        ps = p1 + p2 if p1 else 0
        return (
            "```text\n"
            "┌──────────────────────┐\n"
            "│    DÉS DU DESTIN     │\n"
            "├──────────────────────┤\n"
            f"│ BANQUE : {ds} {mini_dice.get(d1, '?')}{mini_dice.get(d2, '?')}   │\n"
            f"│ {name:<10}: {ps} {mini_dice.get(p1, '?')}{mini_dice.get(p2, '?')}   │\n"
            "├──────────────────────┤\n"
            f"│ {status:<20} │\n"
            f"│ Mise: {format_currency(bet):<14} │\n"
            "└──────────────────────┘\n"
            "```"
        )

    await interaction.followup.send(get_dice_box(None, None, None, None, "Prêt..."), ephemeral=True)
    anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

    for _ in range(4):
        await asyncio.sleep(0.5)
        await anim_manager.update_animation(
            new_content=get_dice_box(
                random.randint(1, 6), random.randint(1, 6),
                random.randint(1, 6), random.randint(1, 6),
                "Roulent..."
            )
        )

    d1, d2 = random.randint(1, 6), random.randint(1, 6)
    p1, p2 = random.randint(1, 6), random.randint(1, 6)
    
    if (p1 + p2) > (d1 + d2):
        update_wallet(interaction.user.id, bet)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        status = f"VICTOIRE! +{format_currency(bet)}"
        await send_public_log(content=f"🎲 **{interaction.user.display_name}** a gagné aux dés ! +**{format_currency(bet)}**")
    elif (p1 + p2) < (d1 + d2):
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        status = f"PERDU! -{format_currency(bet)}"
        await send_public_log(content=f"🎲 **{interaction.user.display_name}** a perdu aux dés ! -**{format_currency(bet)}**")
    else:
        status = "ÉGALITÉ !"

    await anim_manager.update_animation(new_content=get_dice_box(d1, d2, p1, p2, status))


@bot.tree.command(name="dice", description="Joue aux Dés du Destin")
async def dice(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🎲 Dés - Mise", run_dice_game))


async def run_roulette_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "roulette", bet):
        return
    
    show_anim = get_user_animation_preference(interaction.user.id)
    name = interaction.user.display_name.upper()[:10]

    def get_box(num, icon, color, status, choice):
        return (
            "```text\n"
            "┌──────────────────────┐\n"
            "│       ROULETTE       │\n"
            "├──────────────────────┤\n"
            f"│ {name:<10} CHOIX:{choice[:4]} │\n"
            f"│ ROUE: [{icon} {num:02d} {color[:3].upper()}]   │\n"
            "├──────────────────────┤\n"
            f"│ {status:<20} │\n"
            f"│ Mise: {format_currency(bet):<14} │\n"
            "└──────────────────────┘\n"
            "```"
        )

    # Créer une vue avec les boutons
    class RouletteView(ui.View):
        def __init__(self, user_id):
            super().__init__(timeout=30)
            self.user_id = user_id
            self.choice = None

        async def interaction_check(self, interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
                return False
            return True

        async def play(self, interaction, choice):
            self.choice = choice
            for child in self.children:
                child.disabled = True
            
            number = random.randint(0, 36)
            color = "vert" if number == 0 else ("rouge" if number % 2 == 0 else "noir")
            icon = "🟩" if number == 0 else ("🟥" if color == "rouge" else "⬛")
            
            await interaction.response.edit_message(
                content=get_box(number, icon, color, "Tourne...", choice),
                view=self
            )
            
            anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)
            for _ in range(3):
                await asyncio.sleep(0.5)
                rn = random.randint(0, 36)
                rc = "vert" if rn == 0 else ("rouge" if rn % 2 == 0 else "noir")
                ri = "🟩" if rn == 0 else ("🟥" if rc == "rouge" else "⬛")
                await anim_manager.update_animation(
                    new_content=get_box(rn, ri, rc, "Ralentit...", choice)
                )
            
            if choice == color:
                mult = 14 if color == "vert" else 2
                reward = bet * mult
                update_wallet(interaction.user.id, reward - bet)
                update_game_stats(interaction.user.id, won=True)
                await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
                status = f"GAGNÉ! +{format_currency(reward)}"
                await send_public_log(content=f"🎡 **{interaction.user.display_name}** a gagné à la roulette ! +**{format_currency(reward)}**")
            else:
                update_wallet(interaction.user.id, -bet)
                update_game_stats(interaction.user.id, won=False)
                status = f"PERDU! -{format_currency(bet)}"
                await send_public_log(content=f"🎡 **{interaction.user.display_name}** a perdu à la roulette ! -**{format_currency(bet)}**")
            
            await anim_manager.update_animation(
                new_content=get_box(number, icon, color, status, choice)
            )

        @ui.button(label="🟥 Rouge (x2)", style=discord.ButtonStyle.danger)
        async def rouge(self, interaction, button):
            await self.play(interaction, "rouge")

        @ui.button(label="⬛ Noir (x2)", style=discord.ButtonStyle.secondary)
        async def noir(self, interaction, button):
            await self.play(interaction, "noir")

        @ui.button(label="🟩 Vert (x14)", style=discord.ButtonStyle.success)
        async def vert(self, interaction, button):
            await self.play(interaction, "vert")

    view = RouletteView(interaction.user.id)
    await interaction.followup.send(
        content=get_box(None, "🎡", "???", "Choisis une couleur", "?"),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="roulette", description="Joue au Cercle de la Fortune")
async def roulette(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🎡 Roulette - Mise", run_roulette_game))


async def run_blackjack_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "blackjack", bet):
        return
    
    class BlackjackGame:
        def __init__(self):
            self.deck = []
            self.player_hand = []
            self.dealer_hand = []
            self.create_deck()
            
        def create_deck(self):
            suits = ["♠️", "♥️", "♦️", "♣️"]
            ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
            self.deck = [{"rank": r, "suit": s} for s in suits for r in ranks]
            random.shuffle(self.deck)
            
        def draw(self):
            return self.deck.pop()
            
        def score(self, hand):
            total = 0
            aces = 0
            for card in hand:
                r = card["rank"]
                if r in ["J", "Q", "K"]:
                    total += 10
                elif r == "A":
                    aces += 1
                    total += 11
                else:
                    total += int(r)
            while total > 21 and aces:
                total -= 10
                aces -= 1
            return total
            
        def format_hand(self, hand, hide=False):
            if hide:
                return f"[{hand[0]['rank']}{hand[0]['suit']}] [?]"
            return " ".join([f"[{c['rank']}{c['suit']}]" for c in hand])

    game = BlackjackGame()
    game.player_hand = [game.draw(), game.draw()]
    game.dealer_hand = [game.draw(), game.draw()]

    class BlackjackView(ui.View):
        def __init__(self, user_id, bet, game):
            super().__init__(timeout=60)
            self.user_id = user_id
            self.bet = bet
            self.game = game
            self.game_over = False

        async def interaction_check(self, interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
                return False
            return True

        def get_embed(self, hide_dealer=True, result=None):
            player_score = self.game.score(self.game.player_hand)
            dealer_score = self.game.score(self.game.dealer_hand) if not hide_dealer else "?"
            dealer_str = self.game.format_hand(self.game.dealer_hand, hide_dealer)
            
            table = (
                "```text\n"
                "┌────────────────────────┐\n"
                "│      BLACKJACK         │\n"
                "├────────────────────────┤\n"
                "│ BANQUE :               │\n"
                f"│ {dealer_str:<22} │\n"
                f"│ Score : {str(dealer_score):<14} │\n"
                "├────────────────────────┤\n"
                "│ VOUS :                 │\n"
                f"│ {self.game.format_hand(self.game.player_hand):<22} │\n"
                f"│ Score : {str(player_score):<14} │\n"
                "├────────────────────────┤\n"
                f"│ Mise: {format_currency(self.bet):<16} │\n"
                "└────────────────────────┘\n"
                "```"
            )
            embed = discord.Embed(title="👑 BLACKJACK", description=table, color=discord.Color.dark_green())
            if result:
                embed.add_field(name="📋 RÉSULTAT", value=result, inline=False)
            return embed

        @ui.button(label="🃏 Tirer", style=discord.ButtonStyle.primary)
        async def hit(self, interaction, button):
            if self.game_over:
                return
            self.game.player_hand.append(self.game.draw())
            score = self.game.score(self.game.player_hand)
            
            if score > 21:
                self.game_over = True
                for child in self.children:
                    child.disabled = True
                update_wallet(self.user_id, -self.bet)
                update_game_stats(self.user_id, won=False)
                await check_and_unlock_achievements(self.user_id, bot_client=bot)
                await send_public_log(content=f"🃏 **{interaction.user.display_name}** a fait un BUST ! -**{format_currency(self.bet)}**")
                await interaction.response.edit_message(
                    embed=self.get_embed(hide_dealer=False, result=f"💥 BUST ! -{format_currency(self.bet)}"),
                    view=self
                )
            elif score == 21:
                self.game_over = True
                for child in self.children:
                    child.disabled = True
                gain = int(self.bet * 1.5)
                update_wallet(self.user_id, gain)
                update_game_stats(self.user_id, won=True)
                await check_and_unlock_achievements(self.user_id, bot_client=bot)
                await send_public_log(content=f"🃏 **{interaction.user.display_name}** a fait un BLACKJACK ! +**{format_currency(gain)}**")
                await interaction.response.edit_message(
                    embed=self.get_embed(hide_dealer=False, result=f"🎉 BLACKJACK ! +{format_currency(gain)}"),
                    view=self
                )
            else:
                await interaction.response.edit_message(embed=self.get_embed(), view=self)

        @ui.button(label="🛑 Rester", style=discord.ButtonStyle.secondary)
        async def stand(self, interaction, button):
            if self.game_over:
                return
            self.game_over = True
            for child in self.children:
                child.disabled = True
                
            while self.game.score(self.game.dealer_hand) < 17:
                self.game.dealer_hand.append(self.game.draw())
                
            p_score = self.game.score(self.game.player_hand)
            d_score = self.game.score(self.game.dealer_hand)
            
            if d_score > 21:
                gain = self.bet
                update_wallet(self.user_id, gain)
                update_game_stats(self.user_id, won=True)
                await check_and_unlock_achievements(self.user_id, bot_client=bot)
                result = f"🎉 Banque > 21 ! +{format_currency(gain)}"
                await send_public_log(content=f"🃏 **{interaction.user.display_name}** a gagné au Blackjack ! +**{format_currency(gain)}**")
            elif p_score > d_score:
                gain = self.bet
                update_wallet(self.user_id, gain)
                update_game_stats(self.user_id, won=True)
                await check_and_unlock_achievements(self.user_id, bot_client=bot)
                result = f"🎉 Gagné ! +{format_currency(gain)}"
                await send_public_log(content=f"🃏 **{interaction.user.display_name}** a gagné au Blackjack ! +**{format_currency(gain)}**")
            elif p_score < d_score:
                update_wallet(self.user_id, -self.bet)
                update_game_stats(self.user_id, won=False)
                result = f"❌ Perdu ! -{format_currency(self.bet)}"
                await send_public_log(content=f"🃏 **{interaction.user.display_name}** a perdu au Blackjack ! -**{format_currency(self.bet)}**")
            else:
                result = "🤝 Égalité !"
                
            await interaction.response.edit_message(
                embed=self.get_embed(hide_dealer=False, result=result),
                view=self
            )

    view = BlackjackView(interaction.user.id, bet, game)
    await interaction.followup.send(embed=view.get_embed(), view=view, ephemeral=True)


@bot.tree.command(name="blackjack", description="Joue au Vingt-et-Un Royal")
async def blackjack(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("👑 Blackjack - Mise", run_blackjack_game))


async def run_slots_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "slots", bet):
        return

    show_anim = get_user_animation_preference(interaction.user.id)
    symbols = ["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"]
    name = interaction.user.display_name.upper()[:10]

    def get_box(s1, s2, s3, status):
        return (
            "```text\n"
            "┌──────────────────────┐\n"
            "│    COFFRE ÉCUS       │\n"
            "├──────────────────────┤\n"
            f"│ JOUEUR : {name:<11} │\n"
            "│                      │\n"
            f"│      [{s1}] [{s2}] [{s3}]      │\n"
            "│                      │\n"
            "├──────────────────────┤\n"
            f"│ {status:<20} │\n"
            f"│ Mise: {format_currency(bet):<14} │\n"
            "└──────────────────────┘\n"
            "```"
        )

    await interaction.followup.send(get_box("🍒", "🔔", "💎", "Ouverture..."), ephemeral=True)
    anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

    for _ in range(5):
        await asyncio.sleep(0.3)
        await anim_manager.update_animation(
            new_content=get_box(
                random.choice(symbols), random.choice(symbols), random.choice(symbols),
                "Tourne..."
            )
        )

    f1, f2, f3 = random.choice(symbols), random.choice(symbols), random.choice(symbols)

    if f1 == f2 == f3:
        mult = 20 if f1 == "💎" else (10 if f1 == "7️⃣" else 5)
        reward = bet * mult
        status = f"TRIPLE! +{format_currency(reward)}"
        update_wallet(interaction.user.id, reward - bet)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        await send_public_log(content=f"🪙 **{interaction.user.display_name}** a fait un TRIPLE aux slots ! +**{format_currency(reward)}**")
    elif f1 == f2 or f2 == f3 or f1 == f3:
        reward = int(bet * 1.5)
        status = f"DUO! +{format_currency(reward)}"
        update_wallet(interaction.user.id, reward - bet)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        await send_public_log(content=f"🪙 **{interaction.user.display_name}** a fait un DUO aux slots ! +**{format_currency(reward)}**")
    else:
        status = f"PERDU! -{format_currency(bet)}"
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        await send_public_log(content=f"🪙 **{interaction.user.display_name}** a perdu aux slots ! -**{format_currency(bet)}**")

    await anim_manager.update_animation(new_content=get_box(f1, f2, f3, status))


@bot.tree.command(name="slots", description="Joue au Coffre des Mille Écus")
async def slots(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🪙 Slots - Mise", run_slots_game))


async def run_pfc_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "pfc", bet):
        return

    emaps = {"pierre": "🪨 Pierre", "feuille": "📄 Feuille", "ciseau": "✂️ Ciseau"}
    name = interaction.user.display_name.upper()[:10]

    def get_box(uc, bc, status, face):
        return (
            "```text\n"
            "┌──────────────────────┐\n"
            "│         PFC          │\n"
            "├──────────────────────┤\n"
            f"│ {name:<10}: {uc:<9} │\n"
            f"│ BOT     : {bc:<9} │\n"
            f"│        {face}          │\n"
            "├──────────────────────┤\n"
            f"│ {status:<20} │\n"
            f"│ Mise: {format_currency(bet):<14} │\n"
            "└──────────────────────┘\n"
            "```"
        )

    class PFCView(ui.View):
        def __init__(self, user_id):
            super().__init__(timeout=30)
            self.user_id = user_id
            self.choice = None

        async def interaction_check(self, interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
                return False
            return True

        async def play(self, interaction, choice):
            self.choice = choice
            for child in self.children:
                child.disabled = True
                
            await interaction.response.edit_message(
                content=get_box(emaps[choice], "Analyse...", "Duel...", "(._.)"),
                view=self
            )
            
            anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)
            for step in ["Pierre...", "Feuille...", "Ciseau!"]:
                await asyncio.sleep(0.3)
                await anim_manager.update_animation(
                    new_content=get_box(emaps[choice], f"{random.choice(['🪨','📄','✂️'])}...", step, "(>_<)")
                )
            
            bot_choice = random.choice(["pierre", "feuille", "ciseau"])
            
            if choice == bot_choice:
                res = "🤝 Égalité !"
                face = "(^_^;)"
            elif ((choice == "pierre" and bot_choice == "ciseau") or
                  (choice == "feuille" and bot_choice == "pierre") or
                  (choice == "ciseau" and bot_choice == "feuille")):
                update_wallet(self.user_id, bet)
                update_game_stats(self.user_id, won=True)
                await check_and_unlock_achievements(self.user_id, bot_client=bot)
                res = "🎉 Gagné !"
                face = "(^o^) 🏆"
                await send_public_log(content=f"✂️ **{interaction.user.display_name}** a gagné au PFC ! +**{format_currency(bet)}**")
            else:
                update_wallet(self.user_id, -bet)
                update_game_stats(self.user_id, won=False)
                res = "❌ Perdu !"
                face = "(T_T) 💀"
                await send_public_log(content=f"✂️ **{interaction.user.display_name}** a perdu au PFC ! -**{format_currency(bet)}**")
            
            await anim_manager.update_animation(
                new_content=get_box(emaps[choice], emaps[bot_choice], res, face)
            )

        @ui.button(label="🪨 Pierre", style=discord.ButtonStyle.primary)
        async def pierre(self, interaction, button):
            await self.play(interaction, "pierre")

        @ui.button(label="📄 Feuille", style=discord.ButtonStyle.success)
        async def feuille(self, interaction, button):
            await self.play(interaction, "feuille")

        @ui.button(label="✂️ Ciseau", style=discord.ButtonStyle.danger)
        async def ciseau(self, interaction, button):
            await self.play(interaction, "ciseau")

    view = PFCView(interaction.user.id)
    await interaction.followup.send(
        content=get_box("En attente", "En attente", "Choisis", "(o_o)"),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="pfc", description="Joue à Pierre-Feuille-Ciseaux")
async def pfc(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("✂️ PFC - Mise", run_pfc_game))


async def run_russian_roulette(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "roulette-russe", bet):
        return

    class RRView(ui.View):
        def __init__(self, user_id, bet):
            super().__init__(timeout=60)
            self.user_id = user_id
            self.bet = bet
            self.shots = 0
            self.bullet = random.randint(0, 5)
            self.alive = True

        async def interaction_check(self, interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
                return False
            return True

        def get_display(self, status, emoji):
            return (
                "```text\n"
                "┌──────────────────────┐\n"
                "│    ROULETTE RUSSE    │\n"
                "├──────────────────────┤\n"
                f"│ Tir #{self.shots}/6            │\n"
                f"│       {emoji}          │\n"
                "├──────────────────────┤\n"
                f"│ {status:<20} │\n"
                f"│ Mise: {format_currency(self.bet):<14} │\n"
                "└──────────────────────┘\n"
                "```"
            )

        @ui.button(label="🔫 Tirer", style=discord.ButtonStyle.danger)
        async def shoot(self, interaction, button):
            if not self.alive:
                return
                
            self.shots += 1
            
            if self.shots - 1 == self.bullet:
                self.alive = False
                for child in self.children:
                    child.disabled = True
                update_wallet(self.user_id, -self.bet)
                update_game_stats(self.user_id, won=False)
                await send_public_log(content=f"🔫 **{interaction.user.display_name}** s'est fait tirer dessus ! -**{format_currency(self.bet)}**")
                await interaction.response.edit_message(
                    content=self.get_display("PAN ! PERDU", "💥 (x_x) 💥"),
                    view=self
                )
                return
                
            if self.shots >= 6:
                self.alive = False
                for child in self.children:
                    child.disabled = True
                gain = self.bet * 3
                update_wallet(self.user_id, gain)
                update_game_stats(self.user_id, won=True)
                await check_and_unlock_achievements(self.user_id, bot_client=bot)
                await send_public_log(content=f"🔫 **{interaction.user.display_name}** a survécu à 6 tirs ! +**{format_currency(gain)}**")
                await interaction.response.edit_message(
                    content=self.get_display(f"SURVIVANT ! +{format_currency(gain)}", "😎 (🏆)"),
                    view=self
                )
                return
                
            await interaction.response.edit_message(
                content=self.get_display(f"CLIC ! En vie.", "✨ (o_o) 💧"),
                view=self
            )

        @ui.button(label="💰 Encaisser", style=discord.ButtonStyle.success)
        async def cashout(self, interaction, button):
            if self.shots == 0:
                await interaction.response.send_message("❌ Tire au moins une fois !", ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            gain = int(self.bet * (1 + self.shots * 0.5))
            update_wallet(self.user_id, gain)
            update_game_stats(self.user_id, won=True)
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            await send_public_log(content=f"🔫 **{interaction.user.display_name}** a encaissé {self.shots} tirs ! +**{format_currency(gain)}**")
            await interaction.response.edit_message(
                content=self.get_display(f"RETRAIT +{format_currency(gain)}", "(^_-) 💵"),
                view=self
            )

    view = RRView(interaction.user.id, bet)
    await interaction.followup.send(
        content="```text\n┌──────────────────────┐\n│    ROULETTE RUSSE    │\n├──────────────────────┤\n│ Barillet chargé...   │\n│       😎 (?)         │\n├──────────────────────┤\n│ Prêt au destin       │\n│ Mise: {:<14} │\n└──────────────────────┘\n```".format(format_currency(bet)),
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="roulette-russe", description="Joue à la roulette russe")
async def roulette_russe(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🔫 Roulette Russe - Mise", run_russian_roulette))


async def run_poker_game(interaction: discord.Interaction, mise: int):
    if not await validate_game_bet(interaction, "poker-solitaire", mise):
        return
    
    symboles = ["♠️", "♥️", "♦️", "♣️"]
    valeurs = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    main = [f"[{random.choice(valeurs)}{random.choice(symboles)}]" for _ in range(5)]
    v_main = [c[1:-1] for c in main]
    occ = list({v: v_main.count(v) for v in v_main}.values())

    if 4 in occ:
        gain, res = mise * 5, "🔥 CARRÉ !"
    elif 3 in occ and 2 in occ:
        gain, res = mise * 3, "✨ FULL HOUSE !"
    elif 3 in occ:
        gain, res = mise * 2, "🌟 BRELAN !"
    elif occ.count(2) == 2:
        gain, res = int(mise * 1.5), "⭐ DOUBLE PAIRE !"
    elif 2 in occ:
        gain, res = mise, "💫 PAIRE !"
    else:
        gain, res = -mise, "💀 RIEN..."

    if gain > 0:
        update_wallet(interaction.user.id, gain)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        await send_public_log(content=f"⚜️ **{interaction.user.display_name}** a fait un {res} au poker ! +**{format_currency(gain)}**")
    else:
        update_wallet(interaction.user.id, -mise)
        update_game_stats(interaction.user.id, won=False)
        await send_public_log(content=f"⚜️ **{interaction.user.display_name}** a perdu au poker ! -**{format_currency(mise)}**")

    name = interaction.user.display_name.upper()[:10]
    table = (
        "```text\n"
        "┌──────────────────────┐\n"
        "│     JEU NOBLES       │\n"
        "├──────────────────────┤\n"
        f"│ {name:<20} │\n"
        f"│ {' '.join(main):<20} │\n"
        "├──────────────────────┤\n"
        f"│ {res} {format_currency(gain):<12} │\n"
        "└──────────────────────┘\n"
        "```"
    )
    embed = discord.Embed(title="⚜️ POKER SOLITAIRE", description=table, color=discord.Color.gold() if gain >= 0 else discord.Color.dark_red())
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="poker-solitaire", description="Joue au Poker Solitaire des Nobles")
async def poker_solitaire(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("⚜️ Poker - Mise", run_poker_game))


# ==========================================
# 14. COMMANDES DE SETUP
# ==========================================

@bot.tree.command(name="setup", description="[ADMIN] Configure les salons des PNJ")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(ai_type=[
    app_commands.Choice(name="Tous les PNJ (Carrefour)", value="all"),
    app_commands.Choice(name="Banque (DAB)", value="banque"),
    app_commands.Choice(name="Taverne (Jim)", value="taverne"),
    app_commands.Choice(name="Crime (John)", value="crime"),
    app_commands.Choice(name="Bookmaker (Brook)", value="brook"),
    app_commands.Choice(name="Arene (Bob)", value="arene"),
    app_commands.Choice(name="Marchand (Tom)", value="marchand"),
    app_commands.Choice(name="Troubadour (Guillaume)", value="troubadour"),
    app_commands.Choice(name="Succes", value="achievements"),
    app_commands.Choice(name="Quetes quotidiennes", value="quetes")
])
async def setup(interaction: discord.Interaction, ai_type: str, salon: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id

    if ai_type == "all":
        await interaction.followup.send(f"✅ Le Carrefour des PNJ a bien été déployé dans {salon.mention} !", ephemeral=True)
        
        # Jim
        embed_jim = discord.Embed(
            description="🍺 **Jim le Tavernier**\n*(S'essuie les mains sur un torchon)* Bienvenue dans ma taverne !",
            color=0xD35400
        )
        await salon.send(embed=embed_jim, view=JimTavernView())
        
        # John
        embed_john = discord.Embed(
            description="🥷 **Ruelle Sombre — JOHN LE BRIGAND**\n*(S'adosse à un mur lépreux)* T'as l'regard inquiet, l'ami...",
            color=0x2B2D31
        )
        await salon.send(embed=embed_john, view=JohnCrimeView())
        
        # Bob
        embed_bob = discord.Embed(
            description="🏟️ **L'Arène des Combats — BOB**\n*(Affûtant une longue épée)* Bienvenue dans l'arène, guerrier !",
            color=0x992D22
        )
        await salon.send(embed=embed_bob, view=BobArenaView())
        
        # Brook
        odds = generate_brook_odds()
        embed_brook = discord.Embed(
            description=f"📜 **Guichet des Paris — BROOK**\nCanabis (x{odds[1]}), Jolly Jumper (x{odds[2]}), Pégase (x{odds[3]}), Petit Tonnerre (x{odds[4]})",
            color=0x1ABC9C
        )
        await salon.send(embed=embed_brook, view=BrookBookmakerView(odds))
        
        # Marchand
        embed_marchand = discord.Embed(
            title="✨ Bienvenue au Salon du Shop !",
            description="🦊 **Tom le Marchand** est installé ici.\n👉 Clique sur le bouton ci-dessous !",
            color=discord.Color.gold()
        )
        embed_marchand.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")
        await salon.send(embed=embed_marchand, view=PersistentMerchantView())
        
        # Troubadour
        embed_troubadour = discord.Embed(
            title="🪕 Guillaume le Troubadour",
            description="✨ **Guillaume** est arrivé pour conter les épopées.\n👉 Clique pour lui parler !",
            color=discord.Color.purple()
        )
        embed_troubadour.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        await salon.send(embed=embed_troubadour, view=PersistentTroubadourView())

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO ai_channels (guild_id, ai_type, channel_id) VALUES (?, ?, ?)",
                (guild_id, "brook", salon.id)
            )
            conn.commit()
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO ai_channels (guild_id, ai_type, channel_id) VALUES (?, ?, ?)",
            (guild_id, ai_type, salon.id)
        )
        conn.commit()

    if ai_type == "achievements":
        await interaction.followup.send(f"✅ Salon des succès défini sur {salon.mention} !", ephemeral=True)
        embed = discord.Embed(
            title="🏆 Hall des Succès - Actif",
            description="Ce salon affichera les succès débloqués par les membres !",
            color=discord.Color.gold()
        )
        await salon.send(embed=embed)

    elif ai_type == "banque":
        await interaction.followup.send(f"✅ Guichet de la banque installé dans {salon.mention} !", ephemeral=True)
        embed = discord.Embed(
            title="🏦 Banque des IV Sceaux",
            description="*(Un grincement lourd résonne dans la salle forte)*\nBienvenue au guichet automatique.",
            color=0x34495E
        )
        await salon.send(embed=embed, view=BankView())

    elif ai_type == "taverne":
        await interaction.followup.send(f"✅ Jim installé dans {salon.mention} !", ephemeral=True)
        embed = discord.Embed(
            description="🍺 **Jim le Tavernier**\n*(S'essuie les mains)* Bienvenue dans ma taverne !",
            color=0xD35400
        )
        await salon.send(embed=embed, view=JimTavernView())

    elif ai_type == "crime":
        await interaction.followup.send(f"✅ John installé dans {salon.mention} !", ephemeral=True)
        embed = discord.Embed(
            description="🥷 **Ruelle Sombre — JOHN LE BRIGAND**\n*(S'adosse à un mur)* T'as l'regard inquiet...",
            color=0x2B2D31
        )
        await salon.send(embed=embed, view=JohnCrimeView())

    elif ai_type == "brook":
        await interaction.followup.send(f"✅ Brook installée dans {salon.mention} !", ephemeral=True)
        odds = generate_brook_odds()
        embed = discord.Embed(
            description=f"📜 **Guichet des Paris — BROOK**\nCanabis (x{odds[1]}), Jolly Jumper (x{odds[2]}), Pégase (x{odds[3]}), Petit Tonnerre (x{odds[4]})",
            color=0x1ABC9C
        )
        await salon.send(embed=embed, view=BrookBookmakerView(odds))

    elif ai_type == "arene":
        embed = discord.Embed(
            description="🏟️ **L'Arène des Combats — BOB**\n*(Affûtant une longue épée)* Bienvenue dans l'arène !",
            color=0x992D22
        )
        await salon.send(embed=embed, view=BobArenaView())
        await interaction.followup.send(f"✅ Bob installé dans {salon.mention} !", ephemeral=True)

    elif ai_type == "marchand":
        embed = discord.Embed(
            title="✨ Bienvenue au Salon du Shop !",
            description="🦊 **Tom le Marchand** est installé ici.\n👉 Clique sur le bouton ci-dessous !",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")
        await salon.send(embed=embed, view=PersistentMerchantView())
        await interaction.followup.send(f"✅ Tom installé dans {salon.mention} !", ephemeral=True)

    elif ai_type == "troubadour":
        embed = discord.Embed(
            title="🪕 Guillaume le Troubadour",
            description="✨ **Guillaume** est arrivé pour conter les épopées.\n👉 Clique pour lui parler !",
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        await salon.send(embed=embed, view=PersistentTroubadourView())
        await interaction.followup.send(f"✅ Guillaume installé dans {salon.mention} !", ephemeral=True)

    elif ai_type == "quetes":
        await interaction.followup.send(f"✅ Panneau des quêtes installé dans {salon.mention} !", ephemeral=True)
        quests = get_public_quests()
        embed = discord.Embed(
            title="📋 Quêtes du Jour",
            description="**8 quêtes à valider aujourd'hui !**\nClique sur le bouton pour suivre ta progression.",
            color=discord.Color.gold()
        )
        for i, q in enumerate(quests, 1):
            embed.add_field(name=f"{q['label']}", value=f"{q['desc']}\n`⏳ À valider`", inline=False)
        embed.set_footer(text=f"Quêtes du {_today_str()} • Récompense : 500$")
        msg = await salon.send(embed=embed, view=PublicQuestsView())
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO quest_channels (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
                (guild_id, salon.id, msg.id)
            )
            conn.commit()


# ==========================================
# 15. COMMANDES ADMIN
# ==========================================

@bot.tree.command(name="add-money", description="[ADMIN] Ajouter de l'argent")
@app_commands.checks.has_permissions(administrator=True)
async def add_money(interaction: discord.Interaction, membre: discord.Member, montant: int):
    await interaction.response.defer(ephemeral=True)
    update_wallet(membre.id, montant)
    await check_and_unlock_achievements(membre.id, bot_client=bot)
    await interaction.followup.send(f"💰 **{format_currency(montant)}** ajoutés à {membre.mention} !")


@bot.tree.command(name="remove-money", description="[ADMIN] Retirer de l'argent")
@app_commands.checks.has_permissions(administrator=True)
async def remove_money(interaction: discord.Interaction, membre: discord.Member, montant: int):
    await interaction.response.defer(ephemeral=True)
    if montant <= 0:
        return await interaction.followup.send("❌ Le montant doit être supérieur à 0.", ephemeral=True)

    wallet, bank, _, _, _, _, _, _, _ = get_user(membre.id)
    if wallet >= montant:
        update_wallet(membre.id, -montant)
    elif bank >= montant:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET bank = bank - ? WHERE user_id = ?", (montant, membre.id))
            conn.commit()
    else:
        return await interaction.followup.send("❌ Solde insuffisant.", ephemeral=True)

    await interaction.followup.send(f"💸 **{format_currency(montant)}** retirés de {membre.mention} !")


@bot.tree.command(name="reset-cooldowns", description="[ADMIN] Réinitialise les timers")
@app_commands.checks.has_permissions(administrator=True)
async def reset_cooldowns(interaction: discord.Interaction, membre: discord.Member):
    await interaction.response.defer(ephemeral=True)
    clear_cooldown(membre.id)
    await interaction.followup.send(f"⏳ Cooldowns réinitialisés pour {membre.mention}.", ephemeral=True)


@bot.tree.command(name="toggle-cooldowns", description="[ADMIN] Active/désactive les cooldowns")
@app_commands.checks.has_permissions(administrator=True)
async def toggle_cooldowns(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    global TEST_MODE_ENABLED
    TEST_MODE_ENABLED = not TEST_MODE_ENABLED
    if TEST_MODE_ENABLED:
        embed = discord.Embed(title="🛠️ Mode Test Activé", description="Cooldowns désactivés.", color=discord.Color.green())
    else:
        embed = discord.Embed(title="🛠️ Mode Test Désactivé", description="Cooldowns rétablis.", color=discord.Color.red())
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="reload-episodes", description="[ADMIN] Recharge les épisodes depuis GitHub")
@app_commands.checks.has_permissions(administrator=True)
async def reload_episodes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await load_episodes_from_github()
    await interaction.followup.send("✅ Épisodes rechargés !", ephemeral=True)


@bot.tree.command(name="reset-story", description="[ADMIN] Réinitialise les histoires")
@app_commands.checks.has_permissions(administrator=True)
async def reset_story(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM story_progress")
        cursor.execute("DELETE FROM inventory WHERE item_name LIKE '%Relique%'")
        cursor.execute("DELETE FROM user_last_chapter")
        conn.commit()
    await interaction.followup.send("🔄 Histoire réinitialisée !", ephemeral=True)


# ==========================================
# 16. PERSISTENT VIEWS
# ==========================================

class PersistentMerchantView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler au Marchand", style=discord.ButtonStyle.success, emoji="🦊", custom_id="persistent_merchant_talk")
    async def talk_to_merchant(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="🦊 Tom - Le Marchand Ambulant",
            description=f"Oh, bonjour **{interaction.user.display_name}** !\nBienvenue dans ma boutique !\n\n*Qu'est-ce qui t'amène ?*",
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")
        view = ShopDialogueView(interaction.user)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class ShopDialogueView(ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member

    @ui.button(label="🛒 Voir la boutique", style=discord.ButtonStyle.primary, emoji="✨")
    async def open_shop(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type = 'normal'")
        items = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title="🛒 Boutique Normale", color=discord.Color.gold())
        embed.description = "Voici les objets disponibles :"
        for name, price, desc in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        view = DynamicShopView(interaction.user, 'normal')
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ui.button(label="📖 Boutique Histoire", style=discord.ButtonStyle.success, emoji="📜")
    async def open_story_shop(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)

        view = EpisodeShopView(interaction.user)
        current_ep = view.episode_num
        embed = discord.Embed(title=f"📜 Épisode {current_ep}", color=discord.Color.dark_teal())
        embed.description = "Achète les objets de cet épisode !"

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (current_ep,))
        items = cursor.fetchall()
        conn.close()

        for name, price, desc in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ui.button(label="🎒 Mon inventaire", style=discord.ButtonStyle.secondary, emoji="📦")
    async def open_inventory(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_name, quantity FROM inventory WHERE user_id = ?", (interaction.user.id,))
        rows = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title=f"🎒 Inventaire de {interaction.user.display_name}", color=discord.Color.blue())
        if not rows:
            embed.description = "Ton inventaire est vide..."
        else:
            embed.description = "\n".join([f"• **{name}** x`{qty}`" for name, qty in rows])

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="👋 Au revoir", style=discord.ButtonStyle.danger, emoji="🚪")
    async def leave(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)
        await interaction.response.defer()
        await interaction.delete_original_response()


class DynamicShopView(ui.View):
    def __init__(self, member: discord.Member, shop_type: str):
        super().__init__(timeout=60)
        self.member = member
        self.shop_type = shop_type
        self.load_items()

    def load_items(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_key, name, price, role_to_give_id FROM shop_items WHERE shop_type = ?", (self.shop_type,))
        items = cursor.fetchall()
        conn.close()

        for item_key, name, price, role_to_give_id in items:
            button = ui.Button(
                label=f"Acheter {name} ({format_currency(price)})",
                style=discord.ButtonStyle.success,
                custom_id=f"shop_buy_{item_key}"
            )
            button.callback = self.create_callback(item_key)
            self.add_item(button)

    def create_callback(self, item_key: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.member.id:
                return await interaction.response.send_message("❌ Ce n'est pas votre boutique !", ephemeral=True)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name, price, role_to_give_id FROM shop_items WHERE item_key = ?", (item_key,))
            item = cursor.fetchone()
            conn.close()

            if not item:
                return await interaction.response.send_message("❌ Cet article n'existe plus.", ephemeral=True)

            item_name, item_price, role_to_give_id = item
            wallet = get_user(self.member.id)[0]

            if wallet < item_price:
                return await interaction.response.send_message(f"❌ Solde insuffisant ! Il te manque {format_currency(item_price - wallet)}.", ephemeral=True)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET wallet = wallet - ? WHERE user_id = ?", (item_price, self.member.id))
            cursor.execute("""
                INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1)
                ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1
            """, (self.member.id, item_name))
            conn.commit()
            conn.close()

            if REDIS_AVAILABLE and redis_client:
                redis_client.delete(f"user:{self.member.id}")

            feedback = f"✅ Achat réussi ! Tu as acheté **{item_name}** pour {format_currency(item_price)}"
            if role_to_give_id:
                role = interaction.guild.get_role(role_to_give_id)
                if role:
                    try:
                        await self.member.add_roles(role)
                        feedback += f" et le rôle **{role.name}** t'a été attribué !"
                    except discord.Forbidden:
                        feedback += "\n⚠️ *Achat réussi, mais permissions manquantes pour attribuer le rôle.*"

            await interaction.response.send_message(feedback, ephemeral=True)
        return callback


class EpisodeShopView(ui.View):
    def __init__(self, member: discord.Member, episode_num: int = None):
        super().__init__(timeout=120)
        self.member = member
        
        _, last_shop = get_user_last_chapter(member.id)
        if episode_num is None:
            episode_num = last_shop if last_shop else 1
        self.episode_num = episode_num
        
        update_user_last_chapter(member.id, last_shop_episode=episode_num)
        self.load_items()

    def load_items(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_key, name, price FROM shop_items WHERE shop_type = 'episode' AND episode = ?", (self.episode_num,))
        items = cursor.fetchall()

        all_bought = True
        for item_key, name, price in items:
            cursor.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_name=?", (self.member.id, name))
            res = cursor.fetchone()
            has_item = bool(res and res[0] > 0)

            if not has_item:
                all_bought = False

            button = ui.Button(
                label=f"Possédé : {name}" if has_item else f"Acheter {name} ({format_currency(price)})",
                style=discord.ButtonStyle.secondary if has_item else discord.ButtonStyle.success,
                custom_id=f"ep_buy_{item_key}_{self.episode_num}",
                disabled=has_item,
                row=0
            )
            button.callback = self.create_callback(item_key, name, price)
            self.add_item(button)

        if all_bought and len(items) > 0 and self.episode_num < 25:
            next_btn = ui.Button(label="➡️ Épisode Suivant", style=discord.ButtonStyle.primary, custom_id=f"next_ep_{self.episode_num}", row=1)
            next_btn.callback = self.next_episode_callback
            self.add_item(next_btn)
        conn.close()

    def create_callback(self, item_key: str, item_name: str, item_price: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.member.id:
                return await interaction.response.send_message("❌ Ce n'est pas votre boutique !", ephemeral=True)

            wallet = get_user(self.member.id)[0]
            if wallet < item_price:
                return await interaction.response.send_message(f"❌ Solde insuffisant ! Il te manque {format_currency(item_price - wallet)}.", ephemeral=True)

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET wallet = wallet - ? WHERE user_id = ?", (item_price, self.member.id))
            cursor.execute("""
                INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1)
                ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1
            """, (self.member.id, item_name))
            conn.commit()
            conn.close()

            if REDIS_AVAILABLE and redis_client:
                redis_client.delete(f"user:{self.member.id}")

            new_view = EpisodeShopView(self.member, self.episode_num)
            embed = discord.Embed(title=f"📜 Épisode {self.episode_num}", color=discord.Color.dark_teal())
            embed.description = "Achète tous les objets de cet épisode !"

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (self.episode_num,))
            items = cursor.fetchall()
            conn.close()

            for n, p, desc in items:
                embed.add_field(name=n, value=f"Prix : **{format_currency(p)}**\n*{desc}*", inline=False)

            await interaction.response.edit_message(embed=embed, view=new_view)
            await interaction.followup.send(f"✅ Tu as acheté **{item_name}** pour {format_currency(item_price)} !", ephemeral=True)
        return callback

    async def next_episode_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre boutique !", ephemeral=True)

        next_ep = self.episode_num + 1
        update_user_last_chapter(self.member.id, last_shop_episode=next_ep)
        new_view = EpisodeShopView(self.member, next_ep)
        embed = discord.Embed(title=f"📜 Épisode {next_ep}", color=discord.Color.dark_teal())
        embed.description = "Achète tous les objets de cet épisode !"

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (next_ep,))
        items = cursor.fetchall()
        conn.close()

        for n, p, desc in items:
            embed.add_field(name=n, value=f"Prix : **{format_currency(p)}**\n*{desc}*", inline=False)

        await interaction.response.edit_message(embed=embed, view=new_view)


class PersistentTroubadourView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler à Guillaume", style=discord.ButtonStyle.success, emoji="🪕", custom_id="persistent_troubadour_talk")
    async def talk_to_troubadour(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        last_ep, _ = get_user_last_chapter(interaction.user.id)
        view = TroubadourPaginationView(interaction.user, current_ep=last_ep if last_ep else 1)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


class TroubadourPaginationView(ui.View):
    def __init__(self, member: discord.Member, current_ep: int = 1):
        super().__init__(timeout=120)
        self.member = member
        self.current_ep = current_ep
        self.update_components()

    def update_components(self):
        self.clear_items()

        prev_btn = ui.Button(label="◀️ Précédent", style=discord.ButtonStyle.secondary, disabled=(self.current_ep <= 1), row=0)
        prev_btn.callback = self.prev_callback
        self.add_item(prev_btn)

        page_indicator = ui.Button(label=f"Épisode {self.current_ep} / 25", style=discord.ButtonStyle.blurple, disabled=True, row=0)
        self.add_item(page_indicator)

        next_btn = ui.Button(label="Suivant ▶️", style=discord.ButtonStyle.secondary, disabled=(self.current_ep >= 25), row=0)
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

        # Vérifier si l'épisode est débloqué
        user_id = self.member.id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM story_progress WHERE user_id = ? AND episode_id = ?", (user_id, self.current_ep))
        is_unlocked = cursor.fetchone() is not None

        # Vérifier si les prérequis sont remplis
        if self.current_ep == 1:
            has_all_items = True
        else:
            prev_ep = self.current_ep - 1
            cursor.execute("SELECT name FROM shop_items WHERE shop_type='episode' AND episode=?", (prev_ep,))
            prev_items = [row[0] for row in cursor.fetchall()]
            if prev_items:
                cursor.execute(f"""
                    SELECT COUNT(*) FROM inventory 
                    WHERE user_id = ? AND item_name IN ({','.join(['?']*len(prev_items))}) AND quantity > 0
                """, [user_id] + prev_items)
                owned = cursor.fetchone()[0]
                has_all_items = owned >= len(prev_items)
            else:
                has_all_items = True
        conn.close()

        if is_unlocked:
            btn = ui.Button(label="📖 Écouter / Relire", style=discord.ButtonStyle.success, emoji="📜", row=1)
            btn.callback = self.listen_callback
            self.add_item(btn)
        elif self.current_ep == 1:
            btn = ui.Button(label="📖 Écouter (Gratuit)", style=discord.ButtonStyle.success, emoji="📜", row=1)
            btn.callback = self.listen_callback
            self.add_item(btn)
        elif has_all_items:
            btn = ui.Button(label="🎁 Donner les reliques", style=discord.ButtonStyle.primary, emoji="✨", row=1)
            btn.callback = self.give_callback
            self.add_item(btn)
        else:
            btn = ui.Button(label="🔒 Verrouillé", style=discord.ButtonStyle.danger, disabled=True, row=1)
            self.add_item(btn)

    async def prev_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)
        if self.current_ep > 1:
            self.current_ep -= 1
            update_user_last_chapter(self.member.id, last_episode=self.current_ep)
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)
        if self.current_ep < 25:
            self.current_ep += 1
            update_user_last_chapter(self.member.id, last_episode=self.current_ep)
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def listen_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)

        story_text = f"📜 **Récit de l'Épisode {self.current_ep}**\n\n*Guillaume sort son luth et commence à conter...*\n\n*(L'histoire de cet épisode sera bientôt disponible)*"

        if self.current_ep == 1:
            story_text = "📜 **L'Arche - Le Commencement**\n\n*Guillaume accorde son luth et entonne une mélodie ancienne...*\n\n« Au cœur des terres oubliées, là où les vents murmurent les secrets des anciens, se dresse l'Arche. »"

        embed = discord.Embed(
            title=f"📖 Épisode {self.current_ep}",
            description=story_text,
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def give_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)

        user_id = self.member.id
        prev_ep = self.current_ep - 1

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM shop_items WHERE shop_type='episode' AND episode=?", (prev_ep,))
            prev_items = [row[0] for row in cursor.fetchall()]

            cursor.execute("INSERT OR IGNORE INTO story_progress (user_id, episode_id) VALUES (?, ?)", (user_id, self.current_ep))

            for item in prev_items:
                cursor.execute("DELETE FROM inventory WHERE user_id = ? AND item_name = ?", (user_id, item))
            conn.commit()

        update_user_last_chapter(self.member.id, last_episode=self.current_ep)
        self.update_components()

        await send_public_log(content=f"📜 **{self.member.display_name}** a débloqué l'épisode {self.current_ep} !")

        embed = discord.Embed(
            title=f"📖 Épisode {self.current_ep} débloqué !",
            description="*Guillaume prend les reliques et commence son récit...*\n\n*(L'histoire complète sera bientôt disponible)*",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")

        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        user_id = self.member.id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM story_progress WHERE user_id = ? AND episode_id = ?", (user_id, self.current_ep))
        is_unlocked = cursor.fetchone() is not None
        conn.close()

        status = "📖 Débloqué" if is_unlocked else ("📖 Gratuit" if self.current_ep == 1 else "🔒 Verrouillé")

        embed = discord.Embed(
            title=f"🪕 Guillaume le Troubadour — Épisode {self.current_ep}",
            description=f"Statut : {status}\n\nUtilise les boutons ci-dessous pour naviguer.",
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        return embed


# ==========================================
# 17. GESTION DES ERREURS
# ==========================================

async def _global_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    print(f"❌ ERREUR: {type(original).__name__}: {original}")
    traceback.print_exception(type(original), original, original.__traceback__)

    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "❌ Tu n'as pas les permissions nécessaires."
    elif isinstance(error, app_commands.errors.CheckFailure):
        message = "❌ Tu n'es pas autorisé à utiliser cette commande."
    else:
        message = f"❌ Erreur : `{type(original).__name__}`"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass

bot.tree.on_error = _global_app_command_error


# ==========================================
# 18. CHARGEMENT DES ÉPISODES
# ==========================================

EPISODE_TITLES = {}
EPISODE_STORIES = {}
EPISODES_LOADED = False
TOTAL_EPISODES = 25


async def load_episodes_from_github():
    global EPISODE_TITLES, EPISODE_STORIES, EPISODES_LOADED
    # Version simplifiée - les épisodes sont chargés depuis GitHub
    EPISODE_TITLES = {i: f"Épisode {i}" for i in range(1, TOTAL_EPISODES + 1)}
    EPISODE_STORIES = {i: "Histoire à venir..." for i in range(1, TOTAL_EPISODES + 1)}
    EPISODES_LOADED = True
    return True


# ==========================================
# 19. ON_READY
# ==========================================

@bot.event
async def on_ready():
    print(f"🤖 Bot connecté en tant que {bot.user} (ID: {bot.user.id}")
    
    await load_achievements_from_github()
    await load_episodes_from_github()

    try:
        init_db()
        print("💾 Base de données : OK")
    except Exception as e:
        print(f"❌ ERREUR INIT BASE : {e}")

    if not getattr(bot, "_persistent_views_registered", False):
        try:
            bot.add_view(PersistentMerchantView())
            bot.add_view(PersistentTroubadourView())
            bot.add_view(BobArenaView())
            bot.add_view(PublicQuestsView())
            bot._persistent_views_registered = True
            print("✅ Vues persistantes enregistrées")
        except Exception as e:
            print(f"❌ ERREUR VUES : {e}")

    try:
        await asyncio.sleep(2)
        synced = await bot.tree.sync()
        print(f"🌲 {len(synced)} commandes synchronisées")
    except Exception as e:
        print(f"❌ ERREUR SYNCHRO : {e}")

    print("✅ Bot prêt !")


# ==========================================
# 20. LANCEMENT
# ==========================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Token Discord introuvable.")
    bot.run(TOKEN)
