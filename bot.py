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
import redis
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
# REDIS CONNECTION
# ==========================================

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_AVAILABLE = False

try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    REDIS_AVAILABLE = True
    print("✅ Redis connecté avec succès !")
except Exception as e:
    print(f"⚠️ Redis non disponible: {e}, utilisation du cache mémoire")
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
        
        # ========== NOUVELLES TABLES POUR LES QUÊTES ==========
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
        
        # Ancienne table des quêtes (gardée pour compatibilité)
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

        # ========== TABLE POUR LES DONNEES D'ACHIEVEMENTS ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS achievement_data (
                user_id INTEGER PRIMARY KEY,
                games_played INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0,
                games_lost INTEGER DEFAULT 0,
                beers_drunk INTEGER DEFAULT 0,
                crimes_success INTEGER DEFAULT 0,
                crimes_attempts INTEGER DEFAULT 0,
                vault_attempts INTEGER DEFAULT 0,
                vault_success INTEGER DEFAULT 0,
                pmu_wins INTEGER DEFAULT 0,
                pmu_bets INTEGER DEFAULT 0,
                duels_played INTEGER DEFAULT 0,
                duels_won INTEGER DEFAULT 0,
                work_done INTEGER DEFAULT 0,
                bank_deposits INTEGER DEFAULT 0,
                bank_withdrawals INTEGER DEFAULT 0,
                pay_sent INTEGER DEFAULT 0,
                pay_received INTEGER DEFAULT 0,
                quests_completed INTEGER DEFAULT 0,
                quests_claimed INTEGER DEFAULT 0,
                blackjack_wins INTEGER DEFAULT 0,
                slots_wins INTEGER DEFAULT 0,
                roulette_wins INTEGER DEFAULT 0,
                pfc_wins INTEGER DEFAULT 0,
                poker_wins INTEGER DEFAULT 0,
                russian_roulette_survive INTEGER DEFAULT 0,
                dice_wins INTEGER DEFAULT 0,
                arena_fights INTEGER DEFAULT 0,
                arena_wins INTEGER DEFAULT 0,
                larcins_success INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT ''
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
# 3.1. SYSTEME DE DONNEES POUR ACHIEVEMENTS
# ==========================================

def init_achievement_data(user_id: int):
    """Initialise les données d'achievement pour un joueur"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO achievement_data (user_id, last_updated)
            VALUES (?, ?)
        """, (user_id, datetime.datetime.now().isoformat()))
        conn.commit()

def get_achievement_data(user_id: int) -> dict:
    """Récupère toutes les données d'achievement d'un joueur"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM achievement_data WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if row is None:
            init_achievement_data(user_id)
            return get_achievement_data(user_id)
        
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))

def increment_achievement_data(user_id: int, **kwargs):
    """Incrémente les données d'achievement d'un joueur"""
    increments = {k: v for k, v in kwargs.items() if isinstance(v, int) and v > 0}
    if not increments:
        return
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute("INSERT OR IGNORE INTO achievement_data (user_id) VALUES (?)", (user_id,))
        
        set_clause = ", ".join([f"{key} = COALESCE({key}, 0) + ?" for key in increments])
        values = list(increments.values()) + [datetime.datetime.now().isoformat(), user_id]
        
        cursor.execute(f"""
            UPDATE achievement_data 
            SET {set_clause}, last_updated = ?
            WHERE user_id = ?
        """, values)
        
        conn.commit()

def get_achievement_value(user_id: int, field: str) -> int:
    """Récupère une valeur spécifique des données d'achievement"""
    data = get_achievement_data(user_id)
    return data.get(field, 0)


# ==========================================
# 3.2. FONCTIONS DE TRACKING POUR ACHIEVEMENTS
# ==========================================

def track_game_played(user_id: int, won: bool = False, lost: bool = False):
    increments = {"games_played": 1}
    if won:
        increments["games_won"] = 1
    if lost:
        increments["games_lost"] = 1
    increment_achievement_data(user_id, **increments)

def track_beer(user_id: int):
    increment_achievement_data(user_id, beers_drunk=1)

def track_crime(user_id: int, success: bool = False):
    increments = {"crimes_attempts": 1}
    if success:
        increments["crimes_success"] = 1
    increment_achievement_data(user_id, **increments)

def track_vault(user_id: int, success: bool = False):
    increments = {"vault_attempts": 1}
    if success:
        increments["vault_success"] = 1
    increment_achievement_data(user_id, **increments)

def track_pmu(user_id: int, won: bool = False):
    increments = {"pmu_bets": 1}
    if won:
        increments["pmu_wins"] = 1
    increment_achievement_data(user_id, **increments)

def track_duel(user_id: int, won: bool = False):
    increments = {"duels_played": 1}
    if won:
        increments["duels_won"] = 1
    increment_achievement_data(user_id, **increments)

def track_work(user_id: int):
    increment_achievement_data(user_id, work_done=1)

def track_bank_deposit(user_id: int):
    increment_achievement_data(user_id, bank_deposits=1)

def track_pay_sent(user_id: int):
    increment_achievement_data(user_id, pay_sent=1)

def track_quest_claimed(user_id: int):
    increment_achievement_data(user_id, quests_claimed=1)

def track_game_win(user_id: int, game_type: str):
    game_mapping = {
        "blackjack": "blackjack_wins",
        "slots": "slots_wins",
        "roulette": "roulette_wins",
        "pfc": "pfc_wins",
        "poker": "poker_wins",
        "russian_roulette": "russian_roulette_survive",
        "dice": "dice_wins",
        "arena": "arena_wins",
    }
    field = game_mapping.get(game_type)
    if field:
        increment_achievement_data(user_id, **{field: 1})

def track_larcin(user_id: int, success: bool = False):
    if success:
        increment_achievement_data(user_id, larcins_success=1)


# ==========================================
# 3.3. FONCTIONS REDIS
# ==========================================

def invalidate_user_cache(user_id: int):
    if REDIS_AVAILABLE and redis_client:
        redis_client.delete(f"user:{user_id}")
        redis_client.delete(f"user:{user_id}:wallet")
        redis_client.delete(f"user:{user_id}:bank")

def invalidate_leaderboard():
    if REDIS_AVAILABLE and redis_client:
        redis_client.delete("leaderboard:top10")

def get_user_cached(user_id: int):
    if not REDIS_AVAILABLE or not redis_client:
        return get_user(user_id)
    
    cache_key = f"user:{user_id}"
    cached = redis_client.get(cache_key)
    
    if cached:
        try:
            data = json.loads(cached)
            return (
                data.get("wallet", 0),
                data.get("bank", 0),
                data.get("last_daily", 0),
                data.get("streak", 0),
                data.get("beers_today", 0),
                data.get("last_beer_date", ""),
                data.get("games_played", 0),
                data.get("games_won", 0),
                data.get("games_lost", 0)
            )
        except:
            pass
    
    data = get_user(user_id)
    
    cache_data = {
        "wallet": data[0],
        "bank": data[1],
        "last_daily": data[2],
        "streak": data[3],
        "beers_today": data[4],
        "last_beer_date": data[5],
        "games_played": data[6],
        "games_won": data[7],
        "games_lost": data[8]
    }
    if REDIS_AVAILABLE and redis_client:
        redis_client.setex(cache_key, 300, json.dumps(cache_data))
    
    return data

def check_cooldown_redis(user_id: int, command_name: str, duration: int) -> int:
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

def clear_cooldown_redis(user_id: int, command_name: str = None):
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

def update_leaderboard(user_id: int, score: int):
    if REDIS_AVAILABLE and redis_client:
        redis_client.zadd("leaderboard:richest", {str(user_id): score})

def get_top_10_richest():
    if REDIS_AVAILABLE and redis_client:
        return redis_client.zrevrange("leaderboard:richest", 0, 9, withscores=True)
    return None

def get_user_rank(user_id: int):
    if REDIS_AVAILABLE and redis_client:
        rank = redis_client.zrevrank("leaderboard:richest", str(user_id))
        return rank + 1 if rank is not None else None
    return None

def check_rate_limit(user_id: int, action: str, max_requests: int = 5, window: int = 60) -> bool:
    if TEST_MODE_ENABLED:
        return True
    
    if not REDIS_AVAILABLE or not redis_client:
        return True
    
    key = f"rate:{user_id}:{action}"
    count = redis_client.incr(key)
    
    if count == 1:
        redis_client.expire(key, window)
    
    return count <= max_requests

def get_redis_stats():
    if not REDIS_AVAILABLE or not redis_client:
        return None
    
    try:
        return {
            "keys": len(redis_client.keys("*")),
            "memory": redis_client.info("memory")["used_memory_human"],
            "uptime": redis_client.info("server")["uptime_in_seconds"]
        }
    except:
        return None


# ==========================================
# 3.4. FONCTIONS UTILITAIRES
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
    
    invalidate_user_cache(user_id)
    
    if REDIS_AVAILABLE and redis_client:
        wallet, bank, _, _, _, _, _, _, _ = get_user_cached(user_id)
        update_leaderboard(user_id, wallet + bank)
    
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
    
    invalidate_user_cache(user_id)
    
    track_game_played(user_id, won=won, lost=not won)
    
    update_quest_progress(user_id, "games_played", 1)
    update_quest_progress_v2(user_id, "games_played", 1)
    if won:
        update_quest_progress(user_id, "games_won", 1)
        update_quest_progress_v2(user_id, "games_won", 1)
    
    asyncio.create_task(check_and_unlock_achievements(user_id, bot))


def format_currency(amount: int) -> str:
    return f"{amount:,} $".replace(",", " ")


init_db()


# ==========================================
# 3.5. SYSTÈME DE QUÊTES PUBLIC
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
    
    track_quest_claimed(user_id)
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
# 3.6. ANCIEN SYSTÈME DE QUÊTES
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
    
    track_quest_claimed(user_id)
    asyncio.create_task(check_and_unlock_achievements(user_id, bot))

    return {
        "base_reward": base_reward,
        "multiplier": multiplier,
        "quest_streak": quest_streak,
        "total_reward": total_reward,
    }


# ==========================================
# 3.7. SYSTÈME DES ACHIEVEMENTS (STYLE MEE6)
# ==========================================

TIERS_NAMES = {1: "Bronze"}
TIERS_COLORS = {1: "#CD7F32"}

ACHIEVEMENTS_DEFS = {}
ACHIEVEMENTS_LOADED = False
GITHUB_ACHIEVEMENTS_URL = "https://raw.githubusercontent.com/morgandede0-cyber/Bot-IV-Alpha/main/achievements_list.json"


async def load_achievements_from_github():
    """Charge les achievements depuis GitHub"""
    global ACHIEVEMENTS_DEFS, ACHIEVEMENTS_LOADED
    
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
                        print("⚠️ Fichier JSON invalide ou vide")
                        ACHIEVEMENTS_LOADED = False
                        return False
                else:
                    print(f"⚠️ Erreur HTTP {response.status}")
                    ACHIEVEMENTS_LOADED = False
                    return False
    except Exception as e:
        print(f"❌ Erreur chargement succès: {e}")
        ACHIEVEMENTS_LOADED = False
        return False


def evaluate_stat_for_achievement(ach_key: str, user_id: int) -> int:
    """Évalue la progression d'un joueur pour un achievement - Style MEE6"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Récupérer les stats du joueur
        cursor.execute("""
            SELECT games_played, games_won, beers_today
            FROM users WHERE user_id = ?
        """, (user_id,))
        
        row = cursor.fetchone()
        if not row:
            return 0
        
        games_played, games_won, beers_today = row
        games_played = games_played or 0
        games_won = games_won or 0
        beers_today = beers_today or 0
        
        # Compter les quêtes réclamées
        cursor.execute("SELECT COUNT(*) FROM player_quests WHERE user_id = ? AND claimed = 1", (user_id,))
        player_quests = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND claimed = 1", (user_id,))
        daily_quests = cursor.fetchone()[0] or 0
        total_quests = player_quests + daily_quests
        
        # === MAPPING STYLE MEE6 ===
        if ach_key.startswith("quest_") or ach_key.startswith("daily_"):
            return total_quests
        
        if ach_key.startswith("arena_"):
            if "essai" in ach_key or "assidu" in ach_key:
                return games_played
            return games_won
        
        if ach_key.startswith("pmu_"):
            return games_won
        
        if ach_key.startswith("crime_"):
            return games_played
        
        if ach_key.startswith("vault_"):
            return games_played
        
        if ach_key.startswith("duel_"):
            if "premier" in ach_key or "bretteur" in ach_key:
                return games_played
            return games_won
        
        if ach_key.startswith("taverne_"):
            return beers_today
        
        if ach_key.startswith("bank_"):
            return games_played
        
        if ach_key.startswith("larcin_"):
            return games_played
        
        return 0


async def check_and_unlock_achievements(user_id: int, bot_client=None) -> list:
    """Vérifie et débloque les achievements d'un joueur - Style MEE6"""
    global ACHIEVEMENTS_DEFS, ACHIEVEMENTS_LOADED
    
    if not ACHIEVEMENTS_LOADED:
        await load_achievements_from_github()
    
    if not ACHIEVEMENTS_DEFS:
        return []
    
    today = time.strftime("%Y-%m-%d")
    unlocked_now = []
    
    # Récupérer les achievements déjà débloqués
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT achievement_key FROM user_achievements WHERE user_id = ?", (user_id,))
        unlocked_keys = {row[0] for row in cursor.fetchall()}
    
    # Vérifier chaque achievement
    for ach_id, data in ACHIEVEMENTS_DEFS.items():
        ach_key = data.get("key", ach_id)
        
        if ach_key in unlocked_keys:
            continue
        
        # Calculer la progression
        progress = evaluate_stat_for_achievement(ach_key, user_id)
        threshold = data["thresholds"]["1"]
        
        if progress >= threshold:
            reward = data["rewards"]["1"]
            
            update_wallet(user_id, reward)
            
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
                "reward": reward
            })
            
            if bot_client:
                try:
                    channel = bot_client.get_channel(PUBLIC_LOG_CHANNEL_ID)
                    if channel:
                        user = bot_client.get_user(user_id)
                        mention = user.mention if user else f"<@{user_id}>"
                        await channel.send(f"🎉 {mention} a débloqué le succès **{data['title']}** ! (+{format_currency(reward)})")
                except Exception as e:
                    print(f"❌ Erreur notification: {e}")
    
    return unlocked_now


# ==========================================
# 3.8. COMMANDES D'ACHIEVEMENTS
# ==========================================

@bot.tree.command(name="achievements", description="Affiche tes succès et trophées")
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
    for ach_id, data in ACHIEVEMENTS_DEFS.items():
        threshold = data["thresholds"]["1"]
        reward = data["rewards"]["1"]
        descriptions.append(f"**{data['title']}** - {data['desc']} (Seuil: {threshold}, Récompense: {reward}$)")
    
    embed.description = "\n".join(descriptions[:20])
    if len(descriptions) > 20:
        embed.set_footer(text=f"Total: {len(ACHIEVEMENTS_DEFS)} succès • 20 affichés")
    else:
        embed.set_footer(text=f"Total: {len(ACHIEVEMENTS_DEFS)} succès")
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="reload-achievements", description="[ADMIN] Recharge les succès depuis GitHub")
@app_commands.checks.has_permissions(administrator=True)
async def reload_achievements(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    success = await load_achievements_from_github()
    if success:
        embed = discord.Embed(
            title="✅ Succès rechargés",
            description=f"{len(ACHIEVEMENTS_DEFS)} succès chargés depuis GitHub !",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="❌ Échec du rechargement",
            description="Impossible de charger les succès depuis GitHub.",
            color=discord.Color.red()
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="force-check", description="[ADMIN] Force la vérification des achievements")
@app_commands.checks.has_permissions(administrator=True)
async def force_check(interaction: discord.Interaction, membre: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    
    target_id = membre.id if membre else interaction.user.id
    unlocked = await check_and_unlock_achievements(target_id, bot)
    
    if unlocked:
        embed = discord.Embed(
            title="✅ Succès vérifiés",
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


@bot.tree.command(name="reset-achievements", description="[ADMIN] Réinitialise les succès d'un joueur")
@app_commands.checks.has_permissions(administrator=True)
async def reset_achievements(interaction: discord.Interaction, membre: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if membre:
            cursor.execute("DELETE FROM user_achievements WHERE user_id = ?", (membre.id,))
            msg = f"✅ Succès de {membre.mention} réinitialisés !"
        else:
            cursor.execute("DELETE FROM user_achievements")
            msg = "✅ Succès de tous les joueurs réinitialisés !"
        conn.commit()
    
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="achievement-data", description="Affiche les données d'achievement d'un joueur")
async def achievement_data(interaction: discord.Interaction, membre: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    
    target = membre or interaction.user
    data = get_achievement_data(target.id)
    
    embed = discord.Embed(
        title=f"📊 Données d'achievement - {target.display_name}",
        color=discord.Color.blue()
    )
    
    fields = {
        "🎮 Jeux": ["games_played", "games_won", "games_lost"],
        "🍺 Taverne": ["beers_drunk"],
        "🥷 Crime": ["crimes_success", "crimes_attempts"],
        "🔐 Brinks": ["vault_success", "vault_attempts"],
        "🐎 PMU": ["pmu_wins", "pmu_bets"],
        "⚔️ Duels": ["duels_won", "duels_played"],
        "💼 Travail": ["work_done"],
        "🏦 Banque": ["bank_deposits", "bank_withdrawals"],
        "📋 Quêtes": ["quests_completed", "quests_claimed"],
        "🎯 Jeux spéciaux": ["blackjack_wins", "slots_wins", "roulette_wins", "pfc_wins", "poker_wins", "russian_roulette_survive", "dice_wins", "arena_wins"],
    }
    
    for category, field_list in fields.items():
        lines = []
        for field in field_list:
            value = data.get(field, 0)
            if value > 0 or field in ["games_played", "games_won", "games_lost"]:
                label = field.replace("_", " ").title()
                lines.append(f"{label}: **{value}**")
        if lines:
            embed.add_field(name=category, value="\n".join(lines), inline=True)
    
    embed.set_footer(text=f"Dernière mise à jour: {data.get('last_updated', 'Jamais')}")
    
    await interaction.followup.send(embed=embed, ephemeral=True)


# =============================================================
# CHARGEMENT DES ÉPISODES DEPUIS GITHUB
# =============================================================

EPISODE_TITLES = {}
EPISODE_STORIES = {}
EPISODES_LOADED = False

EPISODES_BASE_URL = "https://raw.githubusercontent.com/morgandede0-cyber/Bot-IV-Alpha/main/episodes/"
TOTAL_EPISODES = 30


async def load_episodes_from_github():
    global EPISODE_TITLES, EPISODE_STORIES, EPISODES_LOADED
    
    titles = {}
    stories = {}
    loaded_count = 0
    
    try:
        async with aiohttp.ClientSession() as session:
            for ep_num in range(1, TOTAL_EPISODES + 1):
                filename = f"S1 EP{ep_num}.txt"
                url = EPISODES_BASE_URL + filename
                try:
                    async with session.get(url, timeout=10) as response:
                        if response.status == 200:
                            content = await response.text()
                            lines = content.split('\n')
                            if lines:
                                title = lines[0].strip()
                                story = '\n'.join(lines[1:]).strip()
                                titles[ep_num] = title
                                stories[ep_num] = story
                                loaded_count += 1
                                print(f"✅ Épisode {ep_num} chargé : {title[:30]}...")
                        else:
                            print(f"⚠️ Épisode {ep_num} introuvable (HTTP {response.status})")
                except Exception as e:
                    print(f"❌ Erreur chargement épisode {ep_num}: {e}")
        
        if loaded_count > 0:
            EPISODE_TITLES = titles
            EPISODE_STORIES = stories
            EPISODES_LOADED = True
            print(f"✅ {loaded_count}/{TOTAL_EPISODES} épisodes chargés depuis GitHub")
            return True
        else:
            EPISODE_TITLES = {1: "Épisode 1 — L'Arche"}
            EPISODE_STORIES = {1: "« Une histoire mystérieuse... »"}
            EPISODES_LOADED = True
            return False
    except Exception as e:
        print(f"❌ Erreur chargement épisodes: {e}")
        EPISODE_TITLES = {1: "Épisode 1 — L'Arche"}
        EPISODE_STORIES = {1: "« Une histoire mystérieuse... »"}
        EPISODES_LOADED = True
        return False


@bot.tree.command(name="reload-episodes", description="[ADMIN] Recharge les épisodes depuis GitHub")
@app_commands.checks.has_permissions(administrator=True)
async def reload_episodes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    success = await load_episodes_from_github()
    if success:
        embed = discord.Embed(
            title="✅ Épisodes rechargés",
            description=f"{len(EPISODE_TITLES)} épisodes chargés depuis GitHub avec succès !",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="⚠️ Rechargement partiel",
            description="Certains épisodes n'ont pas pu être chargés.",
            color=discord.Color.orange()
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


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


def get_episode_title(ep_num: int) -> str:
    if EPISODES_LOADED and ep_num in EPISODE_TITLES:
        return EPISODE_TITLES.get(ep_num, f"Épisode {ep_num}")
    return f"Épisode {ep_num}"


# ==========================================
# 4. FONCTIONS UTILITAIRES & HELPERS
# ==========================================

def check_cooldown(user_id: int, command_name: str, duration: int) -> int:
    return check_cooldown_redis(user_id, command_name, duration)


def clear_cooldown(user_id: int, command_name: str = None):
    clear_cooldown_redis(user_id, command_name)


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
    wallet = get_user_cached(interaction.user.id)[0]
    if wallet < bet:
        await reject("❌ Solde insuffisant dans ton portefeuille ! Pense à retirer de l'argent via /banque.")
        return False
    retry_after = check_cooldown_redis(interaction.user.id, command_name, cooldown_sec)
    if retry_after > 0:
        minutes, seconds = divmod(retry_after, 60)
        await reject(f"⏳ Tu dois attendre **{minutes} min et {seconds} sec** avant de pouvoir rejouer.")
        return False
    return True


# ==========================================
# 5. VUE POUR LE PANNEAU PUBLIC DES QUÊTES
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
# 6. MODALES DE MISE
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
# 6.1. SYSTÈMES DE DUEL ENTRE JOUEURS
# ==========================================

DUEL_REFUSED_MESSAGES = [
    "😱 **{opponent}** a eu la trouille et s'est caché sous la table ! {challenger} reste sans adversaire...",
    "🫣 **{opponent}** a préféré sauver sa peau plutôt que d'affronter {challenger} ! Quel couard !",
    "🍗 **{opponent}** s'est enfui en courant, il avait trop peur de perdre ses précieux deniers !",
    "🤡 **{opponent}** a fait le mort, {challenger} attend toujours son duel...",
    "💀 **{opponent}** a réalisé qu'il allait perdre et a préféré faire semblant de ne pas voir le défi !",
    "🐔 **{opponent}** a picoré et s'est envolé ! {challenger} reste avec sa fierté intacte.",
    "😤 **{opponent}** a décliné le duel, trop occupé à compter ses sous dans son coin !",
    "🤣 **{opponent}** a eu la pétoche et s'est planqué derrière le comptoir !",
    "🦆 **{opponent}** a fait le canard et s'est couvert les yeux ! {challenger} attend toujours un vrai guerrier !",
    "🏃‍♂️ **{opponent}** a pris ses jambes à son cou, visiblement il n'était pas prêt pour ce combat !"
]


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
                update_quest_progress_v2(self.challenger.id, "duel_won", 1)
                track_duel(self.challenger.id, won=True)
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
                update_quest_progress_v2(self.opponent.id, "duel_won", 1)
                track_duel(self.opponent.id, won=True)
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
                update_quest_progress_v2(self.challenger.id, "duel_won", 1)
                track_duel(self.challenger.id, won=True)
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
                update_quest_progress_v2(self.opponent.id, "duel_won", 1)
                track_duel(self.opponent.id, won=True)
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
        
        mock_message = random.choice(DUEL_REFUSED_MESSAGES).format(
            opponent=self.opponent.mention,
            challenger=self.challenger.mention
        )
        
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
        wallet_opp, _, _, _, _, _, _, _, _ = get_user_cached(self.opponent.id)
        wallet_chal, _, _, _, _, _, _, _, _ = get_user_cached(self.challenger.id)

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


# ==========================================
# 7. INTERFACES INTERACTIVES & MODALES (BANQUE & DAB)
# ==========================================

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

            wallet, _, _, _, _, _, _, _, _ = get_user_cached(interaction.user.id)
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

            invalidate_user_cache(interaction.user.id)

            update_quest_progress(interaction.user.id, "bank_deposit", 1)
            update_quest_progress_v2(interaction.user.id, "bank_deposit", 1)
            track_bank_deposit(interaction.user.id)
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

            _, bank, _, _, _, _, _, _, _ = get_user_cached(interaction.user.id)
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

            invalidate_user_cache(interaction.user.id)

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


class BankView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="[ 💳 SOLDE ]", style=discord.ButtonStyle.primary, custom_id="persistent_bank:solde")
    async def check_balance(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        wallet, bank, _, _, _, _, _, _, _ = get_user_cached(interaction.user.id)
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


# ==========================================
# 8. INTERFACES DES IA : JIM, JOHN, BROOK & BOB
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

        wallet_chal, _, _, _, _, _, _, _, _ = get_user_cached(interaction.user.id)
        if wallet_chal < bet:
            return await interaction.followup.send("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)

        wallet_opp, _, _, _, _, _, _, _, _ = get_user_cached(self.opponent.id)
        if wallet_opp < bet:
            return await interaction.followup.send(f"❌ {self.opponent.mention} n'a pas assez d'argent dans son portefeuille pour accepter cette mise.", ephemeral=True)

        game_name = "Dés du Destin" if self.game_type == "dice" else "Pierre-Feuille-Ciseaux"
        view = DuelAcceptView(interaction.user, self.opponent, self.game_type, bet, from_jim=True, interaction_ref=interaction)
        
        embed = discord.Embed(
            title="⚔️ DÉFI DE DUEL (TAVERNE)",
            description=(
                f"{interaction.user.mention} défie {self.opponent.mention} à un duel de **{game_name}** sous l'œil de Jim !\n\n"
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


class TavernDuelSelectView(ui.View):
    def __init__(self, game_type: str):
        super().__init__(timeout=60)
        self.add_item(TavernDuelSelect(game_type))


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


class JimTavernView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Commander une Pinte", style=discord.ButtonStyle.primary, emoji="🍺", custom_id="jim_pinte")
    async def pinte(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        retry_after = check_cooldown_redis(user_id, "jim_taverne", 3600)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            msg_text = f'🍺 *Jim te regarde de travers* : "Tu as déjà bu, attends **{minutes}m {seconds}s**."'
            return await interaction.followup.send(msg_text, ephemeral=True)

        wallet, _, _, _, beers_today, last_beer_date, _, _, _ = get_user_cached(user_id)

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

        invalidate_user_cache(user_id)

        track_beer(user_id)
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
            current_wallet, _, _, _, _, _, _, _, _ = get_user_cached(user_id)
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


# ==========================================
# JOHN - BRIGAND
# ==========================================

class JohnRobSelect(ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Choisis la cible à braquer...",
            min_values=1,
            max_values=1,
            custom_id="john_rob_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        
        victim = self.values[0]
        if victim.id == user_id:
            return await interaction.followup.send("❌ Tu ne peux pas te voler toi-même.", ephemeral=True)

        if victim.bot:
            return await interaction.followup.send("❌ Tu ne peux pas braquer un bot.", ephemeral=True)

        retry_after = 0
        if REDIS_AVAILABLE and redis_client:
            key = f"cooldown:{user_id}:john_rob"
            retry_after = redis_client.ttl(key)
        
        if retry_after > 0 and not TEST_MODE_ENABLED:
            minutes, seconds = divmod(retry_after, 60)
            return await interaction.followup.send(f'🗡️ *John* : "Calme tes ardeurs de voleur, attends **{minutes}m {seconds}s**."', ephemeral=True)

        victim_wallet, _, _, _, _, _, _, _, _ = get_user_cached(victim.id)
        thief_wallet, _, _, _, _, _, _, _, _ = get_user_cached(user_id)

        if victim_wallet < 50:
            return await interaction.followup.send(f"🗡️ *John* : \" {victim.mention} n'a pas un sou, c'est une perte de temps.\"", ephemeral=True)

        if REDIS_AVAILABLE and redis_client:
            redis_client.setex(f"cooldown:{user_id}:john_rob", 60, "1")

        roll = random.random()
        
        if roll < 0.4:
            stolen = random.randint(50, int(victim_wallet * 0.7))
            update_wallet(victim.id, -stolen)
            update_wallet(user_id, stolen)
            track_larcin(user_id, success=True)
            await check_and_unlock_achievements(user_id, bot_client=bot)
            
            await send_public_log(
                content=f"🥷 **{interaction.user.display_name}** a réussi à voler **{format_currency(stolen)}** à {victim.display_name} !"
            )
            
            await interaction.followup.send(
                f"🥷 **[JOHN LE BRIGAND]** Tu as réussi à dérober **{format_currency(stolen)}** à {victim.mention} sans qu'il s'en rende compte ! Bien joué !",
                ephemeral=True
            )
            
        elif roll < 0.7:
            await send_public_log(
                content=f"🥷 **{interaction.user.display_name}** a tenté de voler {victim.display_name} mais a échoué !"
            )
            
            await interaction.followup.send(
                f"❌ **[JOHN LE BRIGAND]** Tu as essayé mais {victim.mention} s'est méfié et a gardé son argent bien en sécurité. Pas de butin aujourd'hui.",
                ephemeral=True
            )
            
        else:
            stolen_from_thief = min(random.randint(50, int(thief_wallet * 0.5)), thief_wallet)
            if stolen_from_thief > 0:
                update_wallet(user_id, -stolen_from_thief)
                update_wallet(victim.id, stolen_from_thief)
                
                await send_public_log(
                    content=f"💥 **{interaction.user.display_name}** s'est fait tabasser et dépouiller par {victim.display_name} ! Perte : **{format_currency(stolen_from_thief)}**"
                )
                
                await interaction.followup.send(
                    f"💥 **[JOHN LE BRIGAND]** Mauvais plan ! {victim.mention} t'a repéré et t'a tabassé avant de te dépouiller de **{format_currency(stolen_from_thief)}** !",
                    ephemeral=True
                )
            else:
                await send_public_log(
                    content=f"💥 **{interaction.user.display_name}** s'est fait repérer par {victim.display_name} mais le voleur était trop pauvre pour se faire dépouiller !"
                )
                
                await interaction.followup.send(
                    f"💥 **[JOHN LE BRIGAND]** {victim.mention} t'a repéré et t'a roué de coups, mais t'es trop pauvre pour te faire voler. Ça t'apprendra !",
                    ephemeral=True
                )


class JohnRobView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(JohnRobSelect())


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
            return await interaction.followup.send("❌ Le code doit être exactement composé de 4 chiffres !", ephemeral=True)

        user_id = interaction.user.id

        if user_input == self.secret_code:
            update_wallet(user_id, self.prize)
            track_vault(user_id, success=True)
            await check_and_unlock_achievements(user_id, bot_client=bot)
            
            await send_public_log(
                content=f"🔐 **{interaction.user.display_name}** a réussi le braquage de la Brinks ! Il a empoché **{format_currency(self.prize)}** !"
            )
            
            embed = discord.Embed(
                title="🔐 [BRINKS] COFFRE OUVERT !",
                description=f"🎉 Incroyable ! Tu as trouvé la bonne combinaison **{self.secret_code}** !\nTu récupères le butin de **{format_currency(self.prize)}** !",
                color=discord.Color.green()
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        hints = []
        for i in range(4):
            if user_input[i] == self.secret_code[i]:
                hints.append(f"Chiffre {i+1} (`{user_input[i]}`) : **Bien placé**")
            elif user_input[i] in self.secret_code:
                hints.append(f"Chiffre {i+1} (`{user_input[i]}`) : **Bon mais mauvais endroit**")
            else:
                hints.append(f"Chiffre {i+1} (`{user_input[i]}`) : **Incorrect**")

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
            _, bank, _, _, _, _, _, _, _ = get_user_cached(user_id)
            fine = int(bank * 0.05)
            if fine > 0:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE users SET bank = bank - ? WHERE user_id = ?", (fine, user_id))
                    conn.commit()
                invalidate_user_cache(user_id)

            await send_public_log(
                content=f"🚨 **{interaction.user.display_name}** s'est fait prendre lors d'un braquage de la Brinks ! Amende : **-{format_currency(fine)}**"
            )

            embed = discord.Embed(
                title="🚨 [BRINKS] ARRIVÉE DE LA POLICE !",
                description=(
                    "💥 Trop de temps perdu ! Les forces de l'ordre débarquent en trombe et bouclent la zone.\n"
                    f"Tu t'enfuis de justesse mais la police te saisis une amende de 5% sur ton compte en banque : **-{format_currency(fine)}** !"
                ),
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


class BrinksVaultView(ui.View):
    def __init__(self, prize: int, attempts_left: int, secret_code: str):
        super().__init__(timeout=60)
        self.prize = prize
        self.attempts_left = attempts_left
        self.secret_code = secret_code

    @ui.button(label="Entrer une combinaison", style=discord.ButtonStyle.danger, emoji="🔢", custom_id="brinks_vault_input")
    async def try_code_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrinksVaultModal(self.prize, self.attempts_left, self.secret_code))


class JohnCrimeView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Tenter un Crime", style=discord.ButtonStyle.danger, emoji="🥷", custom_id="john_crime_btn")
    async def crime_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        retry_after = check_cooldown_redis(user_id, "john_crime", 60)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            msg_text = f'🥷 *John* : "Reviens dans **{minutes}m {seconds}s**."'
            return await interaction.followup.send(msg_text, ephemeral=True)

        success = random.choice([True, False])
        wallet, _, _, _, _, _, _, _, _ = get_user_cached(user_id)
        update_quest_progress(user_id, "crime_attempt", 1)
        update_quest_progress_v2(user_id, "crime_attempt", 1)

        if success:
            gain = random.randint(300, 1000)
            update_wallet(user_id, gain)
            track_crime(user_id, success=True)
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

    @ui.button(label="Braquer quelqu'un", style=discord.ButtonStyle.secondary, emoji="🗡️", custom_id="john_rob_btn")
    async def rob_btn(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        retry_after = 0
        if REDIS_AVAILABLE and redis_client:
            key = f"cooldown:{user_id}:john_rob"
            retry_after = redis_client.ttl(key)
        
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            msg_text = f'🗡️ *John* : "Patiente encore **{minutes}m {seconds}s**."'
            return await interaction.response.send_message(msg_text, ephemeral=True)

        embed = discord.Embed(
            title="🗡️ Braquage en cours",
            description="Sélectionne ta cible dans le menu déroulant ci-dessous :",
            color=discord.Color.dark_theme()
        )
        await interaction.response.send_message(embed=embed, view=JohnRobView(), ephemeral=True)

    @ui.button(label="Braquage de la Brinks", style=discord.ButtonStyle.success, emoji="🔐", custom_id="john_vault_btn")
    async def vault_btn(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        retry_after = check_cooldown_redis(user_id, "john_vault", 3600)
        if retry_after > 0:
            hours, remainder = divmod(retry_after, 3600)
            minutes, seconds = divmod(remainder, 60)
            return await interaction.response.send_message(f'🔐 *John* : "Le convoi de la Brinks est surveillé. Attends **{hours}h {minutes}m {seconds}s** avant de replonger."', ephemeral=True)

        update_quest_progress(user_id, "vault_attempt", 1)
        update_quest_progress_v2(user_id, "vault_attempt", 1)
        prize = random.randint(2000, 7500)
        secret_code = f"{random.randint(0, 9)}{random.randint(0, 9)}{random.randint(0, 9)}{random.randint(0, 9)}"

        embed = discord.Embed(
            title="🔐 Braquage de la Brinks",
            description=(
                "*(John t'amène devant un lourd coffre-fort blindé posé à l'arrière d'un fourgon)*\n\n"
                f"Le coffre contient un magot estimé à **{format_currency(prize)}** !\n"
                "Tu disposes de **5 tentatives** pour deviner le code à 4 chiffres. À chaque essai, un indice te guidera.\n"
                "Attention : si tu échoues, la police débarque et te prélève 5% de ton compte bancaire !"
            ),
            color=discord.Color.dark_purple()
        )
        await interaction.response.send_message(embed=embed, view=BrinksVaultView(prize, 5, secret_code), ephemeral=True)


# ==========================================
# BOB LE MAITRE D'ARME
# ==========================================

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
            f"🛡️ **Bob le Maître d'Arme** : `{'🖤' * max(1, int(self.bob_hp / 10))}` ({self.bob_hp}/100)\n\n"
            f"📜 *Action* : {status_msg}"
        )
        embed = discord.Embed(title="🏟️ Arène des IV Sceaux", description=desc, color=color)
        embed.set_footer(text=f"Mise en jeu : {format_currency(self.bet)}")
        return embed

    async def process_turn(self, interaction: discord.Interaction, player_move: str):
        self.round_count += 1
        
        if player_move == "heavy":
            p_dmg = random.randint(18, 32) if random.random() < 0.6 else 0
            p_text = f"Vous abattez une frappe lourde qui inflige **{p_dmg} dégâts** !" if p_dmg > 0 else "Votre frappe lourde a fendu l'air dans le vide !"
        elif player_move == "fast":
            p_dmg = random.randint(10, 18)
            p_text = f"Votre estoc rapide touche Bob pour **{p_dmg} dégâts** !"
        else: 
            p_dmg = 0
            p_text = "Vous adoptez une posture défensive pour parer les coups."

        self.bob_hp = max(0, self.bob_hp - p_dmg)

        if self.bob_hp <= 0:
            for child in self.children:
                child.disabled = True
            gain = self.bet * 2
            update_wallet(self.user_id, gain - self.bet)
            update_game_stats(self.user_id, won=True)
            update_quest_progress_v2(self.user_id, "arena_fight", 1)
            track_game_win(self.user_id, "arena")
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            
            await send_public_log(
                content=f"⚔️ **{interaction.user.display_name}** a vaincu Bob dans l'arène après {self.round_count} rounds et remporte **{format_currency(gain)}** !"
            )
            
            embed = self.build_embed(f"{p_text}\n\n🏆 **VICTOIRE !** Bob s'agenouille, vaincu par votre bravoure ! +**{format_currency(gain)}**", color=discord.Color.green())
            await interaction.response.edit_message(embed=embed, view=self)
            return

        bob_move = random.choice(["heavy", "fast", "bash"])
        if bob_move == "heavy" and player_move != "parry":
            b_dmg = random.randint(15, 25)
            b_text = f"Bob décoche un coup de massue devastateur : **-{b_dmg} PV** !"
        elif bob_move == "fast":
            b_dmg = random.randint(8, 15)
            b_text = f"Bob décoche un coup de dague vif : **-{b_dmg} PV** !"
        else:
            b_dmg = 5 if player_move != "parry" else 0
            b_text = f"Bob assène un coup de bouclier : **-{b_dmg} PV** !" if b_dmg > 0 else "Votre garde parfaite absorbe entièrement l'attaque de Bob !"

        self.player_hp = max(0, self.player_hp - b_dmg)

        if self.player_hp <= 0:
            for child in self.children:
                child.disabled = True
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            
            await send_public_log(
                content=f"💀 **{interaction.user.display_name}** a été vaincu par Bob dans l'arène après {self.round_count} rounds ! (-{format_currency(self.bet)})"
            )
            
            embed = self.build_embed(f"{p_text}\n{b_text}\n\n💀 **DÉFAITE !** Bob vous terrasse d'un ultime coup de taille. -**{format_currency(self.bet)}**", color=discord.Color.dark_red())
            await interaction.response.edit_message(embed=embed, view=self)
            return

        embed = self.build_embed(f"{p_text}\n{b_text}")
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Frappe Lourde (60%)", style=discord.ButtonStyle.danger, emoji="🪓", custom_id="arena_heavy")
    async def heavy_strike(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_turn(interaction, "heavy")

    @ui.button(label="Estoc Rapide (100%)", style=discord.ButtonStyle.primary, emoji="🗡️", custom_id="arena_fast")
    async def fast_strike(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_turn(interaction, "fast")

    @ui.button(label="Posture Défensive", style=discord.ButtonStyle.secondary, emoji="🛡️", custom_id="arena_parry")
    async def parry_stance(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_turn(interaction, "parry")


async def run_arena_fight(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "arene_fight", bet, cooldown_sec=1800):
        return

    update_quest_progress(interaction.user.id, "arena_fight", 1)
    update_quest_progress_v2(interaction.user.id, "arena_fight", 1)
    
    await send_public_log(
        content=f"⚔️ **{interaction.user.display_name}** entre dans l'arène pour affronter Bob ! Mise : **{format_currency(bet)}**"
    )
    
    view = ArenaFightView(interaction.user.id, bet)
    embed = view.build_embed("Le combat commence ! Choisissez votre style d'attaque.", color=discord.Color.orange())
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class BobArenaView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Entrer dans l'Arène (Combattre Bob)", style=discord.ButtonStyle.danger, emoji="⚔️", custom_id="bob_arena_fight")
    async def fight_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BetModal("⚔️ Arène - Mise de Combat", run_arena_fight))

    @ui.button(label="Défier un ami (Duel PvP)", style=discord.ButtonStyle.secondary, emoji="🤺", custom_id="bob_arena_duel")
    async def duel_btn(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title="🤺 Défi de l'Arène - Choix de l'adversaire",
            description=(
                "*(Bob s'écarte et vous tend une arme d'entraînement)*\n\n"
                "Sélectionne le membre que tu souhaites affronter dans le menu déroulant ci-dessous :"
            ),
            color=discord.Color.dark_gold()
        )
        await interaction.response.send_message(embed=embed, view=ArenaDuelSelectView(), ephemeral=True)


class ArenaDuelSelect(ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Choisis ton adversaire pour le duel de l'arène...",
            min_values=1,
            max_values=1,
            custom_id="bob_arena_duel_select"
        )

    async def callback(self, interaction: discord.Interaction):
        opponent = self.values[0]
        if opponent.id == interaction.user.id:
            return await interaction.response.send_message("❌ Tu ne peux pas te défier toi-même !", ephemeral=True)
        if opponent.bot:
            return await interaction.response.send_message("❌ Tu ne peux pas défier un bot !", ephemeral=True)

        await interaction.response.send_modal(ArenaDuelBetModal(opponent))


class ArenaDuelSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(ArenaDuelSelect())


class ArenaDuelBetModal(ui.Modal, title="⚔️ Arène - Mise du Duel"):
    bet_input = ui.TextInput(label="Montant de la mise", placeholder="Ex: 100", required=True, max_length=6)

    def __init__(self, opponent: discord.Member):
        super().__init__()
        self.opponent = opponent

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

        wallet_chal, _, _, _, _, _, _, _, _ = get_user_cached(interaction.user.id)
        if wallet_chal < bet:
            return await interaction.followup.send("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)

        wallet_opp, _, _, _, _, _, _, _, _ = get_user_cached(self.opponent.id)
        if wallet_opp < bet:
            return await interaction.followup.send(f"❌ {self.opponent.mention} n'a pas assez d'argent dans son portefeuille pour accepter ce duel.", ephemeral=True)

        view = DuelAcceptView(interaction.user, self.opponent, "dice", bet, from_jim=False, interaction_ref=interaction)
        
        embed = discord.Embed(
            title="⚔️ DÉFI DE L'ARÈNE",
            description=(
                f"{interaction.user.mention} défie {self.opponent.mention} en duel dans l'arène de Bob !\n\n"
                f"💰 **Mise en jeu** : `{format_currency(bet)}` par joueur\n\n"
                f"{self.opponent.mention}, acceptez-vous ce combat ?\n\n"
                f"⏰ Vous avez **30 secondes** pour répondre !"
            ),
            color=discord.Color.dark_gold()
        )
        view.public_message = await send_public_log(embed=embed, view=view)
        if view.public_message is None:
            return await interaction.followup.send("❌ Impossible d'envoyer la demande de duel dans le Salon B. Vérifie les permissions du bot.", ephemeral=True)
        await interaction.followup.send("✅ Demande de duel envoyée dans le Salon B.", ephemeral=True)


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

    initial_piste = "🏁 **PMU - Départ de la course !** Les chevaux s'élancent...\n```text\n┌── HIPPODROME ────────┐\n"
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

        piste_str = "🏁 **PMU - Course en cours...**\n```text\n┌── HIPPODROME ────────┐\n"
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
        update_quest_progress_v2(interaction.user.id, "pmu_win", 1)
        track_pmu(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        res_msg = f"🏆 **[PMU] VICTOIRE !** #{gagnant} ({chevaux[gagnant]['nom']}) a gagné ! Ton pari sur **{chevaux[cheval]['nom']}** (cote x{cote}) passe haut la main ! +**{format_currency(gain)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a gagné un pari PMU sur **{chevaux[cheval]['nom']}** (x{cote}) et remporte **{format_currency(gain)}** !"
        )
    else:
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        res_msg = f"❌ **[PMU] PERDU !** C'est #{gagnant} ({chevaux[gagnant]['nom']}) qui a gagné. Ton pari sur **{chevaux[cheval]['nom']}** (cote x{cote}) est perdant. -**{format_currency(bet)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a perdu un pari PMU sur **{chevaux[cheval]['nom']}** (x{cote}). Perte : **{format_currency(bet)}**"
        )

    final_piste = "🏁 **PMU - Arrivée de la course !**\n```text\n┌── HIPPODROME ────────┐\n"
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

    initial_piste = "🏁 **Brook - Départ de la course PMU !** Les chevaux s'élancent...\n```text\n┌── HIPPODROME ────────┐\n"
    for cid, data in chevaux.items():
        initial_piste += f"│#{cid}[{data['emoji']}{'-'*piste_len}]│\n"
    initial_piste += "└──────────────────────┘\n```"

    course_message = await interaction.followup.send(initial_piste, ephemeral=True, wait=True)

    async def edit_course(content: str):
        try:
            await course_message.edit(content=content)
        except discord.NotFound:
            print("❌ Le message de course Brook n'existe plus.")
        except discord.HTTPException as e:
            print(f"❌ Erreur animation Brook : {e}")

    weights = [round(10 / dynamic_odds[i], 2) for i in range(1, 5)]

    while max(positions.values()) < piste_len:
        await asyncio.sleep(1.0)
        for c in positions:
            if positions[c] < piste_len:
                positions[c] += random.randint(1, 3)
                if positions[c] > piste_len:
                    positions[c] = piste_len

        piste_str = "🏁 **Brook - Course PMU en cours...**\n```text\n┌── HIPPODROME ────────┐\n"
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
        update_quest_progress_v2(interaction.user.id, "pmu_win", 1)
        track_pmu(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        res_msg = f"🏆 **[BROOK LA BOOKMAKEUSE] VICTOIRE !** #{gagnant} ({chevaux[gagnant]['nom']}) a gagné ! Ton pari sur **{chevaux[horse_choice]['nom']}** (cote x{cote}) passe haut la main ! +**{format_currency(gain)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a gagné un pari Brook sur **{chevaux[horse_choice]['nom']}** (x{cote}) et remporte **{format_currency(gain)}** !"
        )
    else:
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        res_msg = f"❌ **[BROOK LA BOOKMAKEUSE] PERDU !** C'est #{gagnant} ({chevaux[gagnant]['nom']}) qui a gagné. Ton pari sur **{chevaux[horse_choice]['nom']}** (cote x{cote}) est perdant. -**{format_currency(bet)}**"
        
        await send_public_log(
            content=f"🏇 **{interaction.user.display_name}** a perdu un pari Brook sur **{chevaux[horse_choice]['nom']}** (x{cote}). Perte : **{format_currency(bet)}**"
        )

    final_piste = f"🏁 **Brook - Arrivée de la course PMU !**\n```text\n┌── HIPPODROME ────────┐\n"
    for cid, data in chevaux.items():
        p = positions[cid]
        ligne = "-" * p + data["emoji"] + "-" * (piste_len - p)
        final_piste += f"│#{cid}[{ligne}]│\n"
    final_piste += f"└──────────────────────┘\n```\n{res_msg}"

    await edit_course(final_piste)

    new_odds = generate_brook_odds()
    new_embed = discord.Embed(
        description=(
            "📜 **Guichet des Paris — BROOK**\n"
            f"*(Tient un carnet de notes rempli de chiffres)* Bienvenue chez **Brook** ! Les cotes ont varié pour ce tour. "
            f"Clique sur ton cheval favori pour lancer la course animée : Canabis (x{new_odds[1]}), Jolly Jumper (x{new_odds[2]}), Pégase (x{new_odds[3]}) ou Petit Tonnerre (x{new_odds[4]})."
        ),
        color=0x1ABC9C
    )

    try:
        if panel_message is not None:
            await panel_message.edit(embed=new_embed, view=BrookBookmakerView(new_odds))
        else:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT channel_id FROM ai_channels WHERE ai_type = ?", ("brook",))
                row = cur.fetchone()
            if row:
                channel = bot.get_channel(row[0])
                if channel:
                    async for message in channel.history(limit=50):
                        if message.author == bot.user and message.embeds:
                            desc = message.embeds[0].description or ""
                            if "Guichet des Paris — BROOK" in desc:
                                await message.edit(embed=new_embed, view=BrookBookmakerView(new_odds))
                                break
    except Exception as e:
        print(f"❌ Erreur lors de la remise en place des boutons Brook : {type(e).__name__}: {e}")


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


# ==========================================
# 9. COMMANDES D'ÉCONOMIE, BANQUE & ADMIN
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


@bot.tree.command(name="duel", description="Affronter un ami à un jeu de la taverne (/dice ou /pfc)")
@app_commands.choices(game=[
    app_commands.Choice(name="Dés (/dice)", value="dice"),
    app_commands.Choice(name="Pierre-Feuille-Ciseaux (/pfc)", value="pfc")
])
async def duel(interaction: discord.Interaction, opponent: discord.Member, game: str, bet: int):
    await interaction.response.defer(ephemeral=False)
    if opponent.id == interaction.user.id:
        return await interaction.followup.send("❌ Vous ne pouvez pas vous affronter vous-même !", ephemeral=True)
    if opponent.bot:
        return await interaction.followup.send("❌ Vous ne pouvez pas affronter un bot !", ephemeral=True)
    if bet <= 0:
        return await interaction.followup.send("❌ La mise doit être supérieure à 0 $ !", ephemeral=True)
    if bet > MAX_BET:
        return await interaction.followup.send(f"❌ La mise maximale autorisée est de **{MAX_BET} $** !", ephemeral=True)

    wallet_chal, _, _, _, _, _, _, _, _ = get_user_cached(interaction.user.id)
    if wallet_chal < bet:
        return await interaction.followup.send("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)

    wallet_opp, _, _, _, _, _, _, _, _ = get_user_cached(opponent.id)
    if wallet_opp < bet:
        return await interaction.followup.send(f"❌ {opponent.mention} n'a pas assez d'argent dans son portefeuille pour accepter cette mise.", ephemeral=True)

    game_name = "Dés du Destin" if game == "dice" else "Pierre-Feuille-Ciseaux"
    view = DuelAcceptView(interaction.user, opponent, game, bet, from_jim=True, interaction_ref=interaction)

    embed = discord.Embed(
        title="⚔️ DÉFI DE DUEL",
        description=(
            f"{interaction.user.mention} défie {opponent.mention} à un duel de **{game_name}** !\n\n"
            f"💰 **Mise en jeu** : `{format_currency(bet)}` par joueur\n\n"
            f"{opponent.mention}, acceptez-vous ce défi ?\n\n"
            f"⏰ Vous avez **30 secondes** pour répondre !"
        ),
        color=discord.Color.dark_red()
    )
    view.public_message = await send_public_log(embed=embed, view=view)
    if view.public_message is None:
        return await interaction.followup.send("❌ Impossible d'envoyer la demande de duel dans le Salon B. Vérifie les permissions du bot.", ephemeral=True)
    await interaction.followup.send("✅ Demande de duel envoyée dans le Salon B.", ephemeral=True)


@bot.tree.command(name="pmu", description="Parie sur une course de chevaux rapide (PMU)")
async def pmu(interaction: discord.Interaction):
    await interaction.response.send_modal(PMUBetModal())


@bot.tree.command(name="vault", description="Tenter de braquer le coffre de la Brinks")
async def vault(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    retry_after = check_cooldown_redis(user_id, "john_vault", 3600)
    if retry_after > 0:
        hours, remainder = divmod(retry_after, 3600)
        minutes, seconds = divmod(remainder, 60)
        return await interaction.followup.send(f'🔐 *John* : "Le convoi de la Brinks est surveillé. Attends **{hours}h {minutes}m {seconds}s** avant de replonger."', ephemeral=True)

    update_quest_progress(user_id, "vault_attempt", 1)
    update_quest_progress_v2(user_id, "vault_attempt", 1)
    prize = random.randint(2000, 7500)
    secret_code = f"{random.randint(0, 9)}{random.randint(0, 9)}{random.randint(0, 9)}{random.randint(0, 9)}"

    embed = discord.Embed(
        title="🔐 Braquage de la Brinks",
        description=(
            "*(John t'amène devant un lourd coffre-fort blindé posé à l'arrière d'un fourgon)*\n\n"
            f"Le coffre contient un magot estimé à **{format_currency(prize)}** !\n"
            "Tu disposes de **5 tentatives** pour deviner le code à 4 chiffres. À chaque essai, un indice te guidera.\n"
            "Attention : si tu échoues, la police débarque et te prélève 5% de ton compte bancaire !"
        ),
        color=discord.Color.dark_purple()
    )
    await interaction.followup.send(embed=embed, view=BrinksVaultView(prize, 5, secret_code), ephemeral=True)


@bot.tree.command(name="profile", description="Affiche ta carte récapitulative financière, tes statistiques de jeux et ta progression de l'histoire")
async def profile(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user = interaction.user
    wallet, bank, _, streak, beers_today, _, games_played, games_won, games_lost = get_user_cached(user.id)
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


@bot.tree.command(name="richest", description="Affiche l'économie globale du serveur et le classement exact")
async def richest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    
    top_redis = get_top_10_richest()
    
    if top_redis and REDIS_AVAILABLE:
        embed = discord.Embed(title="🏆 Classement", color=0xF1C40F)
        description = ["🌐 **Global** : `Classement Redis`", "──────────────"]
        medals = ["🥇", "🥈", "🥉"]
        
        for index, (user_id, score) in enumerate(top_redis, start=1):
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


@bot.tree.command(name="daily", description="Réclame ta récompense quotidienne")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    user_id = interaction.user.id
    retry_after = check_cooldown_redis(user_id, "daily", 86400)
    if retry_after > 0:
        hours, remainder = divmod(retry_after, 3600)
        minutes, seconds = divmod(remainder, 60)
        return await interaction.followup.send(f"⏳ Déjà réclamé ! Reviens dans **{hours}h {minutes}m {seconds}s**.", ephemeral=True)

    _, _, last_daily, streak, _, _, _, _, _ = get_user_cached(user_id)
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
    
    invalidate_user_cache(user_id)

    embed = discord.Embed(title="🎁 Daily", description=f"Tu as reçu **{format_currency(total_reward)}** !", color=discord.Color.blurple())
    embed.add_field(name="🔥 Série", value=f"**{streak}j**", inline=False)
    if reset_streak:
        embed.add_field(name="⚠️ Réinitialisé", value="> 48h écoulées.", inline=False)

    await interaction.followup.send(embed=embed)


def build_quest_embed(user: discord.abc.User, quests: list, base_reward: int, quest_streak: int) -> discord.Embed:
    multiplier = get_quest_multiplier(quest_streak)
    total_reward = round(base_reward * multiplier)
    all_completed = all(q["progress"] >= q["target"] for q in quests)
    already_claimed = any(q["claimed"] for q in quests)

    embed = discord.Embed(
        title=f"📋 Quêtes Journalières de {user.display_name}",
        description=(
            "Complète ces 5 défis en utilisant les activités des IV Sceaux (jeux, banque, "
            "arène, taverne, crime...) pour gagner des récompenses. Réinitialisation toutes les **24h**.\n"
            "⚠️ La récompense n'est versée que si **TOUTES** les quêtes sont terminées !\n\n"
            f"💰 Cagnotte du jour : **{format_currency(base_reward)}** × Multiplicateur **x{multiplier:.2f}** "
            f"(streak : {quest_streak}j) = **{format_currency(total_reward)}**"
        ),
        color=discord.Color.teal()
    )
    for q in quests:
        progress = min(q["progress"], q["target"])
        if q["claimed"]:
            status = "✅ Récompense réclamée"
        elif progress >= q["target"]:
            status = "🎯 Terminée"
        else:
            status = "⏳ En cours"
        embed.add_field(
            name=status,
            value=f"{q['description']}\nProgression : **{progress}/{q['target']}**",
            inline=False
        )

    if already_claimed:
        footer_text = "Récompense déjà récupérée aujourd'hui • Reviens demain !"
    elif all_completed:
        footer_text = "🎁 Toutes les quêtes sont validées, récupère ta récompense !"
    else:
        footer_text = f"Le multiplicateur augmente avec ton assiduité (plafond x{QUEST_STREAK_MULT_MAX:.2f})."
    embed.set_footer(text=footer_text)
    return embed


class QuestClaimAllButton(ui.Button):
    def __init__(self, quests: list):
        all_completed = all(q["progress"] >= q["target"] for q in quests)
        already_claimed = any(q["claimed"] for q in quests)

        if already_claimed:
            label = "Déjà réclamée ✅"
            style = discord.ButtonStyle.secondary
        elif all_completed:
            label = "Tout Récolter"
            style = discord.ButtonStyle.success
        else:
            nb_done = sum(1 for q in quests if q["progress"] >= q["target"])
            label = f"Quêtes en cours... ({nb_done}/{len(quests)})"
            style = discord.ButtonStyle.secondary

        super().__init__(
            label=label[:80],
            style=style,
            disabled=already_claimed or not all_completed,
            emoji="🎁",
            custom_id="quest_claim_all"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = claim_all_daily_quests(interaction.user.id)
        if result is None:
            return await interaction.followup.send(
                "❌ Toutes les quêtes doivent être terminées pour récupérer la récompense.", ephemeral=True
            )
        if result.get("already_claimed"):
            return await interaction.followup.send(
                "❌ Tu as déjà récupéré la récompense d'aujourd'hui !", ephemeral=True
            )

        quests = get_daily_quests(interaction.user.id)
        base_reward, quest_streak, _ = get_quest_reward_state(interaction.user.id)

        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)

        embed = build_quest_embed(interaction.user, quests, base_reward, quest_streak)
        view = QuestView(quests, base_reward, quest_streak)
        await interaction.message.edit(embed=embed, view=view)
        await interaction.followup.send(
            f"🎉 **Toutes les quêtes sont validées !**\n"
            f"💰 Base : **{format_currency(result['base_reward'])}** × Multiplicateur **x{result['multiplier']:.2f}** "
            f"(streak : {result['quest_streak']}j) = **+{format_currency(result['total_reward'])}**",
            ephemeral=True
        )


class QuestView(ui.View):
    def __init__(self, quests: list, base_reward: int, quest_streak: int):
        super().__init__(timeout=180)
        self.add_item(QuestClaimAllButton(quests))


@bot.tree.command(name="quetes", description="Consulte et réclame tes 5 quêtes journalières")
async def quetes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    quests = get_daily_quests(interaction.user.id)
    base_reward, quest_streak, _ = get_quest_reward_state(interaction.user.id)
    embed = build_quest_embed(interaction.user, quests, base_reward, quest_streak)
    view = QuestView(quests, base_reward, quest_streak)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="achievements", description="Affiche tes succès et trophées sous forme de carte graphique MEE6")
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


@bot.tree.command(name="work", description="Gagne un peu d'argent en travaillant")
async def work(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    user_id = interaction.user.id
    retry_after = check_cooldown_redis(user_id, "work", 3600)
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
    track_work(user_id)
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

    sender_wallet, _, _, _, _, _, _, _, _ = get_user_cached(interaction.user.id)
    if sender_wallet < amount:
        return await interaction.followup.send("❌ Solde insuffisant.", ephemeral=True)

    update_wallet(interaction.user.id, -amount)
    update_wallet(receiver.id, amount)
    update_quest_progress(interaction.user.id, "pay_sent", 1)
    update_quest_progress_v2(interaction.user.id, "pay_sent", 1)
    track_pay_sent(interaction.user.id)
    
    await send_public_log(
        content=f"💸 **{interaction.user.display_name}** a envoyé **{format_currency(amount)}** à {receiver.display_name} !"
    )
    
    await interaction.followup.send(f"💸 {interaction.user.mention} ➔ **{format_currency(amount)}** à {receiver.mention} !", ephemeral=True)


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

        if os.path.exists("assets/jim.png"):
            file_jim = discord.File("assets/jim.png", filename="jim.png")
            embed_jim = discord.Embed(
                description=(
                    "🍺 **Jim le Tavernier**\n"
                    "*(S'essuie les mains sur un torchon taché)* Bienvenue dans ma taverne, voyageur ! Installe-toi près de l'âtre, commande une bonne bière fraîche, ou tente ta chance aux jeux de hasard si le cœur t'en dit."
                ),
                color=0xD35400
            )
            embed_jim.set_image(url="attachment://jim.png")
            await salon.send(embed=embed_jim, file=file_jim, view=JimTavernView())
        else:
            embed_jim = discord.Embed(
                description=(
                    "🍺 **Jim le Tavernier**\n"
                    "*(S'essuie les mains sur un torchon taché)* Bienvenue dans ma taverne, voyageur ! Installe-toi près de l'âtre, commande une bonne bière fraîche, ou tente ta chance aux jeux de hasard si le cœur t'en dit."
                ),
                color=0xD35400
            )
            await salon.send(embed=embed_jim, view=JimTavernView())

        if os.path.exists("assets/john.png"):
            file_john = discord.File("assets/john.png", filename="john.png")
            embed_john = discord.Embed(
                description=(
                    "🥷 **Ruelle Sombre — JOHN LE BRIGAND**\n"
                    "*(S'adosse à un mur lépreux, regard fuyant)* T'as l'regard inquiet, l'ami... Tu cherches à te faire un peu d'fric ou à faire disparaître des emmerdes ? Viens pas pleurer si la milice te tombe dessus."
                ),
                color=0x2B2D31
            )
            embed_john.set_image(url="attachment://john.png")
            await salon.send(embed=embed_john, file=file_john, view=JohnCrimeView())
        else:
            embed_john = discord.Embed(
                description=(
                    "🥷 **Ruelle Sombre — JOHN LE BRIGAND**\n"
                    "*(S'adosse à un mur lépreux, regard fuyant)* T'as l'regard inquiet, l'ami... Tu cherches à te faire un peu d'fric ou à faire disparaître des emmerdes ? Viens pas pleurer si la milice te tombe dessus."
                ),
                color=0x2B2D31
            )
            await salon.send(embed=embed_john, view=JohnCrimeView())

        if os.path.exists("assets/bob.png"):
            file_bob = discord.File("assets/bob.png", filename="bob.png")
            embed_bob = discord.Embed(
                description=(
                    "🏟️ **L'Arène des Combats — BOB LE MAÎTRE D'ARME**\n"
                    "*(Affûtant une longue épée sur une meule de pierre)* Bienvenue dans l'arène, guerrier ! Ici, on teste sa bravoure et son fer. Misez vos deniers et montrez-moi de quoi vous êtes capable !"
                ),
                color=0x992D22
            )
            embed_bob.set_image(url="attachment://bob.png")
            await salon.send(embed=embed_bob, file=file_bob, view=BobArenaView())
        else:
            embed_bob = discord.Embed(
                description=(
                    "🏟️ **L'Arène des Combats — BOB LE MAÎTRE D'ARME**\n"
                    "*(Affûtant une longue épée sur une meule de pierre)* Bienvenue dans l'arène, guerrier ! Ici, on teste sa bravoure et son fer. Misez vos deniers et montrez-moi de quoi vous êtes capable !"
                ),
                color=0x992D22
            )
            await salon.send(embed=embed_bob, view=BobArenaView())

        odds = generate_brook_odds()
        if os.path.exists("assets/brook.png"):
            file_brook = discord.File("assets/brook.png", filename="brook.png")
            embed_brook = discord.Embed(
                description=(
                    "📜 **Guichet des Paris — BROOK**\n"
                    f"*(Tient un carnet de notes rempli de chiffres)* Bienvenue chez **Brook** ! Les cotes ont varié pour ce tour. "
                    f"Clique sur ton cheval favori pour lancer la course animée : Canabis (x{odds[1]}), Jolly Jumper (x{odds[2]}), Pégase (x{odds[3]}) ou Petit Tonnerre (x{odds[4]})."
                ),
                color=0x1ABC9C
            )
            embed_brook.set_image(url="attachment://brook.png")
            await salon.send(embed=embed_brook, file=file_brook, view=BrookBookmakerView(odds))
        else:
            embed_brook = discord.Embed(
                description=(
                    "📜 **Guichet des Paris — BROOK**\n"
                    f"*(Tient un carnet de notes rempli de chiffres)* Bienvenue chez **Brook** ! Les cotes ont varié pour ce tour. "
                    f"Clique sur ton cheval favori pour lancer la course animée : Canabis (x{odds[1]}), Jolly Jumper (x{odds[2]}), Pégase (x{odds[3]}) ou Petit Tonnerre (x{odds[4]})."
                ),
                color=0x1ABC9C
            )
            await salon.send(embed=embed_brook, view=BrookBookmakerView(odds))

        embed_marchand = discord.Embed(
            title="✨ Bienvenue au Salon du Shop !",
            description=(
                "🦊 **Tom le Marchand** est installé ici en permanence.\n\n"
                "👉 **Clique sur le bouton ci-dessous** pour engager la discussion avec lui !"
            ),
            color=discord.Color.gold()
        )
        embed_marchand.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")
        await salon.send(embed=embed_marchand, view=PersistentMerchantView())

        embed_troubadour = discord.Embed(
            title="🪕 Guillaume le Troubadour",
            description=(
                "✨ **Guillaume** est arrivé pour conter les épopées de vos voyages.\n\n"
                "👉 **Clique sur le bouton ci-dessous** pour lui parler et lui donner vos reliques d'épisodes !"
            ),
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
        await interaction.followup.send(f"✅ Le salon des succès débloqués a bien été défini sur {salon.mention} !", ephemeral=True)
        embed = discord.Embed(
            title="🏆 Hall des Succès - Actif",
            description="Ce salon affichera désormais en direct les bannières graphiques de succès débloqués par les membres du serveur !",
            color=discord.Color.gold()
        )
        await salon.send(embed=embed)

    elif ai_type == "banque":
        await interaction.followup.send(f"✅ Le guichet de la banque a pris ses fonctions dans {salon.mention} avec style !", ephemeral=True)
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
        embed.set_footer(text="Banque des IV Sceaux • Service Financier Permanent")
        if os.path.exists("assets/bank.png"):
            file_bank = discord.File("assets/bank.png", filename="bank.png")
            embed.set_image(url="attachment://bank.png")
            await salon.send(embed=embed, file=file_bank, view=BankView())
        else:
            await salon.send(embed=embed, view=BankView())

    elif ai_type == "taverne":
        await interaction.followup.send(f"✅ Jim le tavernier a pris ses fonctions dans {salon.mention} avec style !", ephemeral=True)
        embed = discord.Embed(
            description=(
                "🍺 **Jim le Tavernier**\n"
                "*(S'essuie les mains sur un torchon taché)* Bienvenue dans ma taverne, voyageur ! Installe-toi près de l'âtre, commande une bonne bière fraîche, ou tente ta chance aux jeux de hasard si le cœur t'en dit."
            ),
            color=0xD35400
        )
        if os.path.exists("assets/jim.png"):
            file_jim = discord.File("assets/jim.png", filename="jim.png")
            embed.set_image(url="attachment://jim.png")
            await salon.send(embed=embed, file=file_jim, view=JimTavernView())
        else:
            await salon.send(embed=embed, view=JimTavernView())

    elif ai_type == "crime":
        await interaction.followup.send(f"✅ John le brigand rôde désormais dans {salon.mention} avec style !", ephemeral=True)
        embed = discord.Embed(
            description=(
                "🥷 **Ruelle Sombre — JOHN LE BRIGAND**\n"
                "*(S'adosse à un mur lépreux, regard fuyant)* T'as l'regard inquiet, l'ami... Tu cherches à te faire un peu d'fric ou à faire disparaître des emmerdes ? Viens pas pleurer si la milice te tombe dessus."
            ),
            color=0x2B2D31
        )
        if os.path.exists("assets/john.png"):
            file_john = discord.File("assets/john.png", filename="john.png")
            embed.set_image(url="attachment://john.png")
            await salon.send(embed=embed, file=file_john, view=JohnCrimeView())
        else:
            await salon.send(embed=embed, view=JohnCrimeView())

    elif ai_type == "brook":
        await interaction.followup.send(f"✅ Brook la bookmakeuse a ouvert son guichet dans {salon.mention} avec style !", ephemeral=True)
        odds = generate_brook_odds()
        embed = discord.Embed(
            description=(
                "📜 **Guichet des Paris — BROOK**\n"
                f"*(Tient un carnet de notes rempli de chiffres)* Bienvenue chez **Brook** ! Les cotes ont varié pour ce tour. "
                f"Clique sur ton cheval favori pour lancer la course animée : Canabis (x{odds[1]}), Jolly Jumper (x{odds[2]}), Pégase (x{odds[3]}) ou Petit Tonnerre (x{odds[4]})."
            ),
            color=0x1ABC9C
        )
        if os.path.exists("assets/brook.png"):
            file_brook = discord.File("assets/brook.png", filename="brook.png")
            embed.set_image(url="attachment://brook.png")
            await salon.send(embed=embed, file=file_brook, view=BrookBookmakerView(odds))
        else:
            await salon.send(embed=embed, view=BrookBookmakerView(odds))

    elif ai_type == "arene":
        embed = discord.Embed(
            description=(
                "🏟️ **L'Arène des Combats — BOB LE MAÎTRE D'ARME**\n"
                "*(Affûtant une longue épée sur une meule de pierre)* Bienvenue dans l'arène, guerrier ! Ici, on teste sa bravoure et son fer. Misez vos deniers et montrez-moi de quoi vous êtes capable !"
            ),
            color=0x992D22
        )
        if os.path.exists("assets/bob.png"):
            file_bob = discord.File("assets/bob.png", filename="bob.png")
            embed.set_image(url="attachment://bob.png")
            await salon.send(embed=embed, file=file_bob, view=BobArenaView())
        else:
            await salon.send(embed=embed, view=BobArenaView())
        await interaction.followup.send(f"✅ Bob le maître d'arme a dressé son arène dans {salon.mention} avec panache !", ephemeral=True)

    elif ai_type == "marchand":
        embed = discord.Embed(
            title="✨ Bienvenue au Salon du Shop !",
            description=(
                "🦊 **Tom le Marchand** est installé ici en permanence.\n\n"
                "👉 **Clique sur le bouton ci-dessous** pour engager la discussion avec lui !"
            ),
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")
        await salon.send(embed=embed, view=PersistentMerchantView())
        await interaction.followup.send(f"✅ Tom le Marchand a été installé dans {salon.mention} avec succès !", ephemeral=True)

    elif ai_type == "troubadour":
        embed = discord.Embed(
            title="🪕 Guillaume le Troubadour",
            description=(
                "✨ **Guillaume** est arrivé pour conter les épopées de vos voyages.\n\n"
                "👉 **Clique sur le bouton ci-dessous** pour lui parler et lui donner vos reliques d'épisodes !"
            ),
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        await salon.send(embed=embed, view=PersistentTroubadourView())
        await interaction.followup.send(f"✅ Guillaume le Troubadour a été installé dans {salon.mention} avec succès !", ephemeral=True)

    elif ai_type == "quetes":
        await interaction.followup.send(f"✅ Le panneau des quêtes a été installé dans {salon.mention} avec succès !", ephemeral=True)
        
        quests = get_public_quests()
        
        embed = discord.Embed(
            title=f"📋 Quêtes du Jour",
            description="**8 quêtes sont à valider aujourd'hui !**\n\nTermine toutes les quêtes pour gagner ta récompense.\nClique sur le bouton ci-dessous pour suivre ta progression.",
            color=discord.Color.gold()
        )
        
        for i, q in enumerate(quests, 1):
            embed.add_field(
                name=f"{q['label']}",
                value=f"{q['desc']}\n`⏳ À valider`",
                inline=False
            )
        
        embed.set_footer(text=f"Quêtes du {_today_str()} • Récompense : 500$")
        
        msg = await salon.send(embed=embed, view=PublicQuestsView())
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO quest_channels (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
            (guild_id, salon.id, msg.id)
        )
        conn.commit()
        conn.close()


@bot.tree.command(name="add-money", description="[ADMIN] Ajouter de l'argent")
@app_commands.checks.has_permissions(administrator=True)
async def add_money(interaction: discord.Interaction, membre: discord.Member, montant: int):
    await interaction.response.defer(ephemeral=True)
    update_wallet(membre.id, montant)
    await check_and_unlock_achievements(membre.id, bot_client=bot)
    await interaction.followup.send(f"💰 **{format_currency(montant)}** ajoutés à {membre.mention} !")


@bot.tree.command(name="remove-money", description="[ADMIN] Retirer de l'argent du portefeuille ou de la banque d'un joueur")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(compte=[
    app_commands.Choice(name="Portefeuille", value="wallet"),
    app_commands.Choice(name="Banque", value="bank"),
])
async def remove_money(interaction: discord.Interaction, membre: discord.Member, compte: str, montant: int):
    await interaction.response.defer(ephemeral=True)
    if montant <= 0:
        return await interaction.followup.send("❌ Le montant doit être supérieur à 0.", ephemeral=True)

    get_user(membre.id)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        if compte == "wallet":
            cursor.execute(
                "UPDATE users SET wallet = MAX(0, COALESCE(wallet, 0) - ?) WHERE user_id = ?",
                (montant, membre.id),
            )
        else:
            cursor.execute(
                "UPDATE users SET bank = MAX(0, COALESCE(bank, 0) - ?) WHERE user_id = ?",
                (montant, membre.id),
            )
        conn.commit()
    
    invalidate_user_cache(membre.id)

    wallet, bank, _, _, _, _, _, _, _ = get_user_cached(membre.id)
    compte_label = "portefeuille" if compte == "wallet" else "bank"
    embed = discord.Embed(
        title="💸 Retrait Administrateur",
        description=(
            f"**{format_currency(montant)}** retirés du **{compte_label}** de {membre.mention}.\n\n"
            f"Nouveau solde — Portefeuille : **{format_currency(wallet)}** | Banque : **{format_currency(bank)}**"
        ),
        color=discord.Color.red()
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="reset-cooldowns", description="[ADMIN] Réinitialise les timers")
@app_commands.checks.has_permissions(administrator=True)
async def reset_cooldowns(interaction: discord.Interaction, membre: discord.Member):
    await interaction.response.defer(ephemeral=True)
    clear_cooldown_redis(membre.id)
    await interaction.followup.send(f"⏳ Cooldowns réinitialisés pour {membre.mention}.", ephemeral=True)


@bot.tree.command(name="toggle-cooldowns", description="[ADMIN] Active ou désactive globalement tous les cooldowns (mode test)")
@app_commands.checks.has_permissions(administrator=True)
async def toggle_cooldowns(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    global TEST_MODE_ENABLED
    TEST_MODE_ENABLED = not TEST_MODE_ENABLED
    if TEST_MODE_ENABLED:
        embed = discord.Embed(
            title="🛠️ Mode Test Activé",
            description="Les cooldowns de toutes les commandes et interactions sont désormais **désactivés**.",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="🛠️ Mode Test Désactivé",
            description="Le fonctionnement normal des cooldowns a été **rétabli**.",
            color=discord.Color.red()
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ==========================================
# 10. JEUX DE CASINO - AVEC TRACKING DES ACHIEVEMENTS
# ==========================================

class BlackjackGame:
    def __init__(self, user_id: int, bet: int):
        self.user_id = user_id
        self.bet = bet
        self.deck = self.create_deck()
        self.player_hand = [self.draw_card(), self.draw_card()]
        self.dealer_hand = [self.draw_card(), self.draw_card()]

    def create_deck(self):
        suits = ["♠️", "♥️", "♦️", "♣️"]
        ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        deck = [{"rank": r, "suit": s} for s in suits for r in ranks]
        random.shuffle(deck)
        return deck

    def draw_card(self):
        return self.deck.pop()

    @staticmethod
    def calculate_score(hand):
        score = 0
        aces = 0
        for card in hand:
            r = card["rank"]
            if r in ["J", "Q", "K"]:
                score += 10
            elif r == "A":
                aces += 1
                score += 11
            else:
                score += int(r)
        while score > 21 and aces:
            score -= 10
            aces -= 1
        return score

    @staticmethod
    def format_hand(hand, hide_second=False):
        if hide_second:
            return f"[{hand[0]['rank']}{hand[0]['suit']}] [?]"
        return " ".join([f"[{c['rank']}{c['suit']}]" for c in hand])


class BlackjackView(ui.View):
    def __init__(self, game: BlackjackGame):
        super().__init__(timeout=60)
        self.game = game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message("❌ Ce n'est pas votre partie !", ephemeral=True)
            return False
        return True

    def build_embed(self, title="👑 BLACKJACK", game_over=False, result_message=""):
        player_score = BlackjackGame.calculate_score(self.game.player_hand)
        if game_over:
            dealer_score = BlackjackGame.calculate_score(self.game.dealer_hand)
            dealer_str = BlackjackGame.format_hand(self.game.dealer_hand)
        else:
            dealer_score = "?"
            dealer_str = BlackjackGame.format_hand(self.game.dealer_hand, hide_second=True)

        table_design = (
            "```text\n"
            "┌────────────────────────┐\n"
            "│      BLACKJACK         │\n"
            "├────────────────────────┤\n"
            "│ BANQUE :               │\n"
            f"│ {dealer_str:<22} │\n"
            f"│ Score : {str(dealer_score):<14} │\n"
            "├────────────────────────┤\n"
            "│ VOUS :                 │\n"
            f"│ {BlackjackGame.format_hand(self.game.player_hand):<22} │\n"
            f"│ Score : {str(player_score):<14} │\n"
            "├────────────────────────┤\n"
            f"│ Mise: {format_currency(self.game.bet):<16} │\n"
            "└────────────────────────┘\n"
            "```"
        )

        embed = discord.Embed(title=title, description=table_design, color=discord.Color.dark_green() if not game_over else discord.Color.green())
        if result_message:
            embed.add_field(name="RÉSULTAT", value=result_message, inline=False)
        return embed

    @ui.button(label="[ 🃏 Tirer ]", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: ui.Button):
        self.game.player_hand.append(self.game.draw_card())
        player_score = BlackjackGame.calculate_score(self.game.player_hand)

        if player_score == 21:
            for child in self.children:
                child.disabled = True
            gain = int(self.game.bet * 1.5)
            update_wallet(self.game.user_id, gain)
            update_game_stats(self.game.user_id, won=True)
            update_quest_progress_v2(self.game.user_id, "blackjack_win", 1)
            track_game_win(self.game.user_id, "blackjack")
            await check_and_unlock_achievements(self.game.user_id, bot_client=bot)
            
            await send_public_log(
                content=f"🃏 **{interaction.user.display_name}** a fait un Blackjack ! +**{format_currency(gain)}**"
            )
            
            embed = self.build_embed(game_over=True, result_message=f"🎉 21 ! +{format_currency(gain)}")
            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()
        elif player_score > 21:
            for child in self.children:
                child.disabled = True
            update_wallet(self.game.user_id, -self.game.bet)
            update_game_stats(self.game.user_id, won=False)
            
            await send_public_log(
                content=f"🃏 **{interaction.user.display_name}** a fait un BUST au Blackjack ! -**{format_currency(self.game.bet)}**"
            )
            
            embed = self.build_embed(game_over=True, result_message=f"💥 BUST ! -{format_currency(self.game.bet)}")
            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()
        else:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @ui.button(label="[ 🛑 Rester ]", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True

        while BlackjackGame.calculate_score(self.game.dealer_hand) < 17:
            self.game.dealer_hand.append(self.game.draw_card())

        player_score = BlackjackGame.calculate_score(self.game.player_hand)
        dealer_score = BlackjackGame.calculate_score(self.game.dealer_hand)

        if dealer_score > 21:
            gain = self.game.bet
            update_wallet(self.game.user_id, gain)
            update_game_stats(self.game.user_id, won=True)
            update_quest_progress_v2(self.game.user_id, "blackjack_win", 1)
            track_game_win(self.game.user_id, "blackjack")
            await check_and_unlock_achievements(self.game.user_id, bot_client=bot)
            res = f"🎉 Banque > 21 ! +{format_currency(gain)}"
            
            await send_public_log(
                content=f"🃏 **{interaction.user.display_name}** a gagné au Blackjack ! +**{format_currency(gain)}**"
            )
        elif player_score > dealer_score:
            gain = self.game.bet
            update_wallet(self.game.user_id, gain)
            update_game_stats(self.game.user_id, won=True)
            update_quest_progress_v2(self.game.user_id, "blackjack_win", 1)
            track_game_win(self.game.user_id, "blackjack")
            await check_and_unlock_achievements(self.game.user_id, bot_client=bot)
            res = f"🎉 Gagné ! +{format_currency(gain)}"
            
            await send_public_log(
                content=f"🃏 **{interaction.user.display_name}** a gagné au Blackjack ! +**{format_currency(gain)}**"
            )
        elif player_score < dealer_score:
            update_wallet(self.game.user_id, -self.game.bet)
            update_game_stats(self.game.user_id, won=False)
            res = "❌ Perdu !"
            
            await send_public_log(
                content=f"🃏 **{interaction.user.display_name}** a perdu au Blackjack ! -**{format_currency(self.game.bet)}**"
            )
        else:
            res = "🤝 Égalité !"

        embed = self.build_embed(game_over=True, result_message=res)
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


async def run_blackjack_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "blackjack", bet):
        return
    game = BlackjackGame(interaction.user.id, bet)
    view = BlackjackView(game)
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="blackjack", description="Joue au Vingt-et-Un Royal")
async def blackjack(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("👑 Blackjack - Mise", run_blackjack_game))


async def run_slots_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "slots", bet):
        return

    show_anim = get_user_animation_preference(interaction.user.id)
    symbols = ["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"]
    name = interaction.user.display_name.upper()[:10]

    def get_slots_box(s1, s2, s3, status):
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

    embed = discord.Embed(title="🪙 SLOTS", description=get_slots_box("🍒", "🔔", "💎", "Ouverture..."), color=0xD4AF37)
    await interaction.followup.send(embed=embed, ephemeral=True)
    anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

    for _ in range(5):
        await asyncio.sleep(0.3)
        rs1, rs2, rs3 = random.choice(symbols), random.choice(symbols), random.choice(symbols)
        anim_embed = discord.Embed(title="🪙 SLOTS", description=get_slots_box(rs1, rs2, rs3, "Tourne..."), color=0xD4AF37)
        await anim_manager.update_animation(new_embed=anim_embed)

    f1, f2, f3 = random.choice(symbols), random.choice(symbols), random.choice(symbols)

    if f1 == f2 == f3:
        mult = 20 if f1 == "💎" else (10 if f1 == "7️⃣" else 5)
        reward = bet * mult
        status = f"TRIPLE! +{format_currency(reward)}"
        update_wallet(interaction.user.id, reward - bet)
        update_game_stats(interaction.user.id, won=True)
        update_quest_progress_v2(interaction.user.id, "slots_win", 1)
        track_game_win(interaction.user.id, "slots")
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        
        await send_public_log(
            content=f"🪙 **{interaction.user.display_name}** a fait un TRIPLE aux slots ! +**{format_currency(reward)}**"
        )
    elif f1 == f2 or f2 == f3 or f1 == f3:
        reward = int(bet * 1.5)
        status = f"DUO! +{format_currency(reward)}"
        update_wallet(interaction.user.id, reward - bet)
        update_game_stats(interaction.user.id, won=True)
        update_quest_progress_v2(interaction.user.id, "slots_win", 1)
        track_game_win(interaction.user.id, "slots")
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        
        await send_public_log(
            content=f"🪙 **{interaction.user.display_name}** a fait un DUO aux slots ! +**{format_currency(reward)}**"
        )
    else:
        status = f"PERDU! -{format_currency(bet)}"
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        
        await send_public_log(
            content=f"🪙 **{interaction.user.display_name}** a perdu aux slots ! -**{format_currency(bet)}**"
        )

    final_embed = discord.Embed(title="🪙 SLOTS", description=get_slots_box(f1, f2, f3, status), color=0xD4AF37)
    if not show_anim:
        try:
            await interaction.edit_original_response(embed=final_embed)
        except discord.HTTPException:
            pass
    else:
        await anim_manager.update_animation(new_embed=final_embed)


@bot.tree.command(name="slots", description="Joue au Coffre des Mille Écus")
async def slots(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🪙 Slots - Mise", run_slots_game))


class DiceView(ui.View):
    def __init__(self, user_id: int, bet: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.bet = bet

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
            return False
        return True

    @ui.button(label="[ 🎲 LANCER ]", style=discord.ButtonStyle.primary, custom_id="dice_roll_btn")
    async def roll_dice(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True

        show_anim = get_user_animation_preference(self.user_id)
        mini_dice = {1: "[⚀]", 2: "[⚁]", 3: "[⚂]", 4: "[⚃]", 5: "[⚄]", 6: "[⚅]", "hidden": "[?]"}
        name = interaction.user.display_name.upper()[:10]

        def get_dice_box(d1, d2, p1, p2, status):
            ds = str(d1 + d2) if d1 and d2 else "?"
            ps = str(p1 + p2) if p1 and p2 else "?"
            de1 = mini_dice[d1] if d1 else mini_dice["hidden"]
            de2 = mini_dice[d2] if d2 else mini_dice["hidden"]
            pl1 = mini_dice[p1] if p1 else mini_dice["hidden"]
            pl2 = mini_dice[p2] if p2 else mini_dice["hidden"]

            return (
                "```ansi\n"
                "\u001b[1;33m┌──────────────────────┐\n"
                "│    DÉS DU DESTIN     │\n"
                "├──────────────────────┤\n"
                f"│ BANQUE : {ds} {de1}{de2}   │\n"
                f"│ {name:<10}: {ps} {pl1}{pl2}   │\n"
                "├──────────────────────┤\n"
                f"│ \u001b[1;36m{status:<20}\u001b[0m\n"
                f"│ Mise: {format_currency(self.bet):<14} │\n"
                "\u001b[1;33m└──────────────────────┘\u001b[0m\n"
                "```"
            )

        await interaction.response.edit_message(content=get_dice_box(None, None, None, None, "Préparation..."), view=self)
        anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

        for _ in range(4):
            await asyncio.sleep(0.6)
            await anim_manager.update_animation(
                new_content=get_dice_box(random.randint(1, 6), random.randint(1, 6), random.randint(1, 6), random.randint(1, 6), "Roulent..."),
                view=self
            )

        d1, d2 = random.randint(1, 6), random.randint(1, 6)
        p1, p2 = random.randint(1, 6), random.randint(1, 6)
        if (p1 + p2) > (d1 + d2):
            update_wallet(self.user_id, self.bet)
            update_game_stats(self.user_id, won=True)
            update_quest_progress_v2(self.user_id, "dice_win", 1)
            track_game_win(self.user_id, "dice")
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            status = f"VICTOIRE! +{format_currency(self.bet)}"
            
            await send_public_log(
                content=f"🎲 **{interaction.user.display_name}** a gagné aux dés ! +**{format_currency(self.bet)}**"
            )
        elif (p1 + p2) < (d1 + d2):
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            status = f"PERDU! -{format_currency(self.bet)}"
            
            await send_public_log(
                content=f"🎲 **{interaction.user.display_name}** a perdu aux dés ! -**{format_currency(self.bet)}**"
            )
        else:
            status = "ÉGALITÉ !"

        final_content = get_dice_box(d1, d2, p1, p2, status)
        if not show_anim:
            try:
                await interaction.edit_original_response(content=final_content, view=self)
            except discord.HTTPException:
                pass
        else:
            await anim_manager.update_animation(new_content=final_content, view=self)


async def run_dice_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "dice", bet):
        return
    name = interaction.user.display_name.upper()[:10]
    initial_box = (
        "```ansi\n"
        "\u001b[1;33m┌──────────────────────┐\n"
        "│    DÉS DU DESTIN     │\n"
        "├──────────────────────┤\n"
        "│ BANQUE : ? [?] [?]   │\n"
        f"│ {name:<10}: ? [?] [?]   │\n"
        "├──────────────────────┤\n"
        "│ \u001b[1;36mPrêt à lancer         \u001b[0m\n"
        f"│ Mise: {format_currency(bet):<14} │\n"
        "\u001b[1;33m└──────────────────────┘\u001b[0m\n"
        "```"
    )
    view = DiceView(interaction.user.id, bet)
    await interaction.followup.send(content=initial_box, view=view, ephemeral=True)


@bot.tree.command(name="dice", description="Joue aux Dés du Destin")
async def dice(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🎲 Dés du Destin - Mise", run_dice_game))


class RouletteView(ui.View):
    def __init__(self, user_id: int, bet: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.bet = bet

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
            return False
        return True

    async def play_roulette(self, interaction: discord.Interaction, choice: str):
        for child in self.children:
            child.disabled = True

        show_anim = get_user_animation_preference(self.user_id)
        wheel_sequence = [(0, "🟩", "vert"), (32, "🟥", "rouge"), (15, "⬛", "noir"), (19, "🟥", "rouge"), (4, "⬛", "noir")]
        name = interaction.user.display_name.upper()[:10]

        def get_box(num, icon, col_name, status):
            return (
                "```ansi\n"
                "\u001b[1;33m┌──────────────────────┐\n"
                "│       ROULETTE       │\n"
                "├──────────────────────┤\n"
                f"│ {name:<10} CHOIX:{choice[:4]} │\n"
                f"│ ROUE: [{icon} {num:02d} ({col_name[:3].upper()})]  │\n"
                "├──────────────────────┤\n"
                f"│ \u001b[1;36m{status:<20}\u001b[0m\n"
                f"│ Mise: {format_currency(self.bet):<14} │\n"
                "\u001b[1;33m└──────────────────────┘\u001b[0m\n"
                "```"
            )

        fn, fi, fc = random.choice(wheel_sequence)
        await interaction.response.edit_message(content=get_box(fn, fi, fc, "Tourne..."), view=self)
        anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

        for _ in range(3):
            await asyncio.sleep(0.7)
            rn, ri, rc = random.choice(wheel_sequence)
            await anim_manager.update_animation(new_content=get_box(rn, ri, rc, "Ralentit..."), view=self)

        number = random.randint(0, 36)
        color = "vert" if number == 0 else ("rouge" if number % 2 == 0 else "noir")
        icon = "🟩" if number == 0 else ("🟥" if color == "rouge" else "⬛")

        if choice == color:
            mult = 14 if color == "vert" else 2
            reward = self.bet * mult
            update_wallet(self.user_id, reward - self.bet)
            update_game_stats(self.user_id, won=True)
            update_quest_progress_v2(self.user_id, "roulette_win", 1)
            track_game_win(self.user_id, "roulette")
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            status = f"GAGNÉ! +{format_currency(reward)}"
            
            await send_public_log(
                content=f"🎡 **{interaction.user.display_name}** a gagné à la roulette ({color}) ! +**{format_currency(reward)}**"
            )
        else:
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            status = f"PERDU! -{format_currency(self.bet)}"
            
            await send_public_log(
                content=f"🎡 **{interaction.user.display_name}** a perdu à la roulette ({choice}) ! -**{format_currency(self.bet)}**"
            )

        final_content = get_box(number, icon, color, status)
        if not show_anim:
            try:
                await interaction.edit_original_response(content=final_content, view=self)
            except discord.HTTPException:
                pass
        else:
            await anim_manager.update_animation(new_content=final_content, view=self)

    @ui.button(label="🟥 Rouge (x2)", style=discord.ButtonStyle.danger, custom_id="roulette_rouge")
    async def rouge(self, interaction: discord.Interaction, button: ui.Button):
        await self.play_roulette(interaction, "rouge")

    @ui.button(label="⬛ Noir (x2)", style=discord.ButtonStyle.secondary, custom_id="roulette_noir")
    async def noir(self, interaction: discord.Interaction, button: ui.Button):
        await self.play_roulette(interaction, "noir")

    @ui.button(label="🟩 Vert (x14)", style=discord.ButtonStyle.success, custom_id="roulette_vert")
    async def vert(self, interaction: discord.Interaction, button: ui.Button):
        await self.play_roulette(interaction, "vert")


async def run_roulette_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "roulette", bet):
        return
    name = interaction.user.display_name.upper()[:10]
    initial_box = (
        "```ansi\n"
        "\u001b[1;33m┌──────────────────────┐\n"
        "│       ROULETTE       │\n"
        "├──────────────────────┤\n"
        f"│ {name:<10} CHOIX: ?     │\n"
        "│ ROUE: [🎡 ATTENTE]   │\n"
        "├──────────────────────┤\n"
        "│ \u001b[1;36mChoisis une couleur  \u001b[0m\n"
        f"│ Mise: {format_currency(bet):<14} │\n"
        "\u001b[1;33m└──────────────────────┘\u001b[0m\n"
        "```"
    )
    view = RouletteView(interaction.user.id, bet)
    await interaction.followup.send(content=initial_box, view=view, ephemeral=True)


@bot.tree.command(name="roulette", description="Joue au Cercle de la Fortune")
async def roulette(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🎡 Roulette - Mise", run_roulette_game))


class RussianRouletteView(ui.View):
    def __init__(self, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.bet = bet
        self.current_shot = 0
        self.bullet_chamber = random.randint(0, 4)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
            return False
        return True

    def get_current_multiplier(self) -> float:
        return {0: 1.0, 1: 1.5, 2: 2.5, 3: 4.0, 4: 7.0, 5: 12.0}.get(self.current_shot, 1.0)

    @ui.button(label="[ 🔫 TIRE ]", style=discord.ButtonStyle.danger, custom_id="rr_shoot")
    async def shoot(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_shot == self.bullet_chamber:
            for child in self.children:
                child.disabled = True
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            
            await send_public_log(
                content=f"🔫 **{interaction.user.display_name}** s'est fait tirer dessus à la roulette russe ! -**{format_currency(self.bet)}**"
            )
            
            display = (
                "```ansi\n"
                "\u001b[1;31m┌──────────────────────┐\n"
                "│    ROULETTE RUSSE    │\n"
                "├──────────────────────┤\n"
                f"│ Tentative #{self.current_shot + 1}/5          │\n"
                "│       💥 (x_x) 💥    │\n"
                "├──────────────────────┤\n"
                "│ \u001b[1;31mPAN ! PERDU           \u001b[0m\n"
                f"│ Perte: -{format_currency(self.bet):<13} │\n"
                "\u001b[1;31m└──────────────────────┘\u001b[0m\n"
                "```"
            )
            await interaction.response.edit_message(content=display, view=self)
            self.stop()
            return

        self.current_shot += 1
        if self.current_shot >= 5:
            for child in self.children:
                child.disabled = True
            total_gain = self.bet + 2000
            update_wallet(self.user_id, total_gain)
            update_game_stats(self.user_id, won=True)
            update_quest_progress_v2(self.user_id, "russian_roulette_survive", 1)
            track_game_win(self.user_id, "russian_roulette")
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            
            await send_public_log(
                content=f"🔫 **{interaction.user.display_name}** a survécu à 5 tirs de roulette russe et remporte **{format_currency(total_gain)}** !"
            )
            
            display = (
                "```ansi\n"
                "\u001b[1;33m┌──────────────────────┐\n"
                "│    CHANCE DU COCU    │\n"
                "├──────────────────────┤\n"
                "│ 5 tirs réussis !     │\n"
                "│       😎 (🏆)        │\n"
                "├──────────────────────┤\n"
                "│ \u001b[1;32mSURVIVANT LÉGENDAIRE \u001b[0m\n"
                f"│ Gain: +{format_currency(total_gain):<14} │\n"
                "\u001b[1;33m└──────────────────────┘\u001b[0m\n"
                "```"
            )
            await interaction.response.edit_message(content=display, view=self)
            self.stop()
            return

        pot = int(self.bet * self.get_current_multiplier())
        display = (
            "```ansi\n"
            "\u001b[1;36m┌──────────────────────┐\n"
            "│    ROULETTE RUSSE    │\n"
            "├──────────────────────┤\n"
            f"│ Tir #{self.current_shot}/5 validé    │\n"
            "│       ✨ (o_o) 💧    │\n"
            "├──────────────────────┤\n"
            "│ \u001b[1;32mCLIC ! En vie.       \u001b[0m\n"
            f"│ Potentiel: {format_currency(pot):<10} │\n"
            "\u001b[1;36m└──────────────────────┘\u001b[0m\n"
            "```"
        )
        await interaction.response.edit_message(content=display, view=self)

    @ui.button(label="[ 💰 Encaisser ]", style=discord.ButtonStyle.success, custom_id="rr_cashout")
    async def cashout(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_shot == 0:
            return await interaction.response.send_message("❌ Tire au moins une fois !", ephemeral=True)
        for child in self.children:
            child.disabled = True
        won = int(self.bet * self.get_current_multiplier())
        update_wallet(self.user_id, won)
        update_game_stats(self.user_id, won=True)
        update_quest_progress_v2(self.user_id, "russian_roulette_survive", 1)
        track_game_win(self.user_id, "russian_roulette")
        await check_and_unlock_achievements(self.user_id, bot_client=bot)
        
        await send_public_log(
            content=f"🔫 **{interaction.user.display_name}** a encaissé après {self.current_shot} tirs de roulette russe ! +**{format_currency(won)}**"
        )
        
        display = (
            "```ansi\n"
            "\u001b[1;32m┌──────────────────────┐\n"
            "│      ENCAISSEMENT    │\n"
            "├──────────────────────┤\n"
            f"│ Tirs réussis : {self.current_shot}/5   │\n"
            "│       (^_-) 💵       │\n"
            "├──────────────────────┤\n"
            "│ \u001b[1;32mRETRAIT SUCCÈS       \u001b[0m\n"
            f"│ Gain: +{format_currency(won):<14} │\n"
            "\u001b[1;32m└──────────────────────┘\u001b[0m\n"
            "```"
        )
        await interaction.response.edit_message(content=display, view=self)
        self.stop()


async def run_russian_roulette(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "roulette-russe", bet):
        return
    display = (
        "```ansi\n"
        "\u001b[1;31m┌──────────────────────┐\n"
        "│    ROULETTE RUSSE    │\n"
        "├──────────────────────┤\n"
        "│ Barillet chargé...   │\n"
        "│       😎 (?)         │\n"
        "├──────────────────────┤\n"
        "│ \u001b[1;33mPrêt au destin       \u001b[0m\n"
        f"│ Mise: {format_currency(bet):<14} │\n"
        "\u001b[1;31m└──────────────────────┘\u001b[0m\n"
        "```"
    )
    view = RussianRouletteView(interaction.user.id, bet)
    await interaction.followup.send(content=display, view=view, ephemeral=True)


@bot.tree.command(name="roulette-russe", description="Joue à la roulette russe")
async def roulette_russe(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("🔫 Roulette Russe - Mise", run_russian_roulette))


class PFCView(ui.View):
    def __init__(self, user_id: int, bet: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.bet = bet
        self.choice = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Pas votre partie !", ephemeral=True)
            return False
        return True

    @ui.button(label="Pierre", style=discord.ButtonStyle.primary, emoji="🪨", custom_id="pfc_pierre")
    async def pierre(self, interaction: discord.Interaction, button: ui.Button):
        self.choice = "pierre"
        self.stop()
        await self.execute_game(interaction)

    @ui.button(label="Feuille", style=discord.ButtonStyle.success, emoji="📄", custom_id="pfc_feuille")
    async def feuille(self, interaction: discord.Interaction, button: ui.Button):
        self.choice = "feuille"
        self.stop()
        await self.execute_game(interaction)

    @ui.button(label="Ciseau", style=discord.ButtonStyle.danger, emoji="✂️", custom_id="pfc_ciseau")
    async def ciseau(self, interaction: discord.Interaction, button: ui.Button):
        self.choice = "ciseau"
        self.stop()
        await self.execute_game(interaction)

    async def execute_game(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True

        show_anim = get_user_animation_preference(self.user_id)
        emaps = {"pierre": "🪨 Pierre", "feuille": "📄 Feuille", "ciseau": "✂️ Ciseau"}
        name = interaction.user.display_name.upper()[:10]

        def get_pfc_box(uc, bc, status, emoji_face):
            return (
                "```ansi\n"
                "\u001b[1;35m┌──────────────────────┐\n"
                "│         PFC          │\n"
                "├──────────────────────┤\n"
                f"│ {name:<10}: {uc:<9} │\n"
                f"│ BOT     : {bc:<9} │\n"
                f"│        {emoji_face}          │\n"
                "├──────────────────────┤\n"
                f"│ \u001b[1;36m{status:<20}\u001b[0m\n"
                f"│ Mise: {format_currency(self.bet):<14} │\n"
                "\u001b[1;35m└──────────────────────┘\u001b[0m\n"
                "```"
            )

        await interaction.response.edit_message(content=get_pfc_box(emaps[self.choice], "Analyse...", "Duel...", "(._.)"), view=self)
        anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

        for step in ["Pierre...", "Feuille...", "Ciseau!"]:
            await asyncio.sleep(0.3)
            await anim_manager.update_animation(new_content=get_pfc_box(emaps[self.choice], f"{random.choice(['🪨','📄','✂️'])}...", step, "(>_<)"), view=self)

        bot_choice = random.choice(["pierre", "feuille", "ciseau"])
        bc_str = emaps[bot_choice]

        if self.choice == bot_choice:
            res = "🤝 Égalité !"
            face = "(^_^;)"
        elif ((self.choice == "pierre" and bot_choice == "ciseau") or
              (self.choice == "feuille" and bot_choice == "pierre") or
              (self.choice == "ciseau" and bot_choice == "feuille")):
            update_wallet(self.user_id, self.bet)
            update_game_stats(self.user_id, won=True)
            update_quest_progress_v2(self.user_id, "pfc_win", 1)
            track_game_win(self.user_id, "pfc")
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            res = "🎉 Gagné !"
            face = "(^o^) 🏆"
            
            await send_public_log(
                content=f"✂️ **{interaction.user.display_name}** a gagné au PFC ! +**{format_currency(self.bet)}**"
            )
        else:
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            res = "❌ Perdu !"
            face = "(T_T) 💀"
            
            await send_public_log(
                content=f"✂️ **{interaction.user.display_name}** a perdu au PFC ! -**{format_currency(self.bet)}**"
            )

        final_content = get_pfc_box(emaps[self.choice], bc_str, res, face)
        if not show_anim:
            try:
                await interaction.edit_original_response(content=final_content, view=self)
            except discord.HTTPException:
                pass
        else:
            await anim_manager.update_animation(new_content=final_content, view=self)


async def run_pfc_game(interaction: discord.Interaction, bet: int):
    if not await validate_game_bet(interaction, "pfc", bet):
        return
    name = interaction.user.display_name.upper()[:10]
    initial_box = (
        "```ansi\n"
        "\u001b[1;35m┌──────────────────────┐\n"
        "│         PFC          │\n"
        "├──────────────────────┤\n"
        f"│ {name:<10}: En attente│\n"
        "│ BOT     : En attente│\n"
        "│        (o_o) ✂️       │\n"
        "├──────────────────────┤\n"
        "│ \u001b[1;36mChoisis ci-dessous   \u001b[0m\n"
        f"│ Mise: {format_currency(bet):<14} │\n"
        "\u001b[1;35m└──────────────────────┘\u001b[0m\n"
        "```"
    )
    view = PFCView(interaction.user.id, bet)
    await interaction.followup.send(content=initial_box, view=view, ephemeral=True)


@bot.tree.command(name="pfc", description="Joue à Pierre-Feuille-Ciseaux")
async def pfc(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("✂️ PFC - Mise", run_pfc_game))


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
        update_quest_progress_v2(interaction.user.id, "poker_win", 1)
        track_game_win(interaction.user.id, "poker")
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        
        await send_public_log(
            content=f"⚜️ **{interaction.user.display_name}** a fait un {res} au poker ! +**{format_currency(gain)}**"
        )
    else:
        update_wallet(interaction.user.id, -mise)
        update_game_stats(interaction.user.id, won=False)
        
        await send_public_log(
            content=f"⚜️ **{interaction.user.display_name}** a perdu au poker ({res}) ! -**{format_currency(mise)}**"
        )

    name = interaction.user.display_name.upper()[:10]

    table_design = (
        "```text\n"
        "┌──────────────────────┐\n"
        "│     JEU NOBLES       │\n"
        "├──────────────────────┤\n"
        f"│ {name:<20} │\n"
        f"│ {' '.join(main):<20} │\n"
        "├──────────────────────┤\n"
        f"│ Mise: {format_currency(mise):<14} │\n"
        "└──────────────────────┘\n"
        "```"
    )
    embed = discord.Embed(title="⚜️ POKER SOLITAIRE", description=table_design, color=discord.Color.gold() if gain >= 0 else discord.Color.dark_red())
    embed.add_field(name="RÉSULTAT", value=f"{res} (**{format_currency(gain)}**)", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="poker-solitaire", description="Joue au Poker Solitaire des Nobles")
async def poker_solitaire(interaction: discord.Interaction):
    await interaction.response.send_modal(BetModal("⚜️ Poker Solitaire - Mise", run_poker_game))


# =============================================================
# SYSTÈME BOUTIQUE & GUILLAUME LE TROUBADOUR
# =============================================================

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

            owned = has_item

            if not owned:
                all_bought = False

            button = ui.Button(
                label=f"Possédé : {name}" if owned else f"Acheter {name} ({format_currency(price)})",
                style=discord.ButtonStyle.secondary if owned else discord.ButtonStyle.success,
                custom_id=f"ep_buy_{item_key}_{self.episode_num}",
                disabled=owned,
                row=0
            )
            button.callback = self.create_callback(item_key, name, price)
            self.add_item(button)

        if all_bought and len(items) > 0 and self.episode_num < TOTAL_EPISODES:
            next_btn = ui.Button(
                label="➡️ Épisode Suivant", 
                style=discord.ButtonStyle.primary, 
                custom_id=f"next_ep_{self.episode_num}",
                row=1
            )
            next_btn.callback = self.next_episode_callback
            self.add_item(next_btn)
        conn.close()

    def create_callback(self, item_key: str, item_name: str, item_price: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.member.id:
                return await interaction.response.send_message("❌ Ce n'est pas votre boutique !", ephemeral=True)

            wallet = get_user_cached(self.member.id)[0]
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

            invalidate_user_cache(self.member.id)

            new_view = EpisodeShopView(self.member, self.episode_num)

            title = get_episode_title(self.episode_num)
            embed = discord.Embed(title=f"📜 {title}", color=discord.Color.dark_teal())
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

        title = get_episode_title(next_ep)
        embed = discord.Embed(title=f"📜 {title}", color=discord.Color.dark_teal())
        embed.description = "Achète tous les objets de cet épisode !"

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (next_ep,))
        items = cursor.fetchall()
        conn.close()

        for n, p, desc in items:
            embed.add_field(name=n, value=f"Prix : **{format_currency(p)}**\n*{desc}*", inline=False)

        await interaction.response.edit_message(embed=embed, view=new_view)


class DynamicShopView(ui.View):
    def __init__(self, member: discord.Member, shop_type: str):
        super().__init__(timeout=60)
        self.member = member
        self.shop_type = shop_type
        self.load_items()

    def load_items(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_key, name, price, required_role_id FROM shop_items WHERE shop_type = ?", (self.shop_type,))
        items = cursor.fetchall()
        conn.close()

        for item_key, name, price, required_role_id in items:
            if required_role_id is not None:
                role = self.member.guild.get_role(required_role_id)
                if not role or role not in self.member.roles:
                    continue

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
            wallet = get_user_cached(self.member.id)[0]

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

            invalidate_user_cache(self.member.id)

            feedback_extra = ""
            if role_to_give_id:
                role_to_give = interaction.guild.get_role(role_to_give_id)
                if role_to_give:
                    try:
                        await self.member.add_roles(role_to_give)
                        feedback_extra = f" et le rôle **{role_to_give.name}** t'a été attribué !"
                    except discord.Forbidden:
                        feedback_extra = "\n⚠️ *Achat réussi, mais le bot manque de permissions pour attribuer le rôle.*"

            await interaction.response.send_message(f"✅ Achat réussi ! Tu as acheté **{item_name}** pour {format_currency(item_price)}{feedback_extra}", ephemeral=True)
        return callback


class ShopDialogueView(ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member

        has_special_access = False
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT required_role_id FROM shop_items WHERE shop_type = 'special'")
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            r_id = row[0]
            if r_id:
                r = self.member.guild.get_role(r_id)
                if r and r in self.member.roles:
                    has_special_access = True
                    break

        if has_special_access:
            special_button = ui.Button(
                label="✨ Boutique Spéciale (Inédits)",
                style=discord.ButtonStyle.blurple,
                custom_id="shop_dialogue_special"
            )
            special_button.callback = self.open_special_shop
            self.add_item(special_button)

    @ui.button(label="🛒 Voir la boutique", style=discord.ButtonStyle.primary, emoji="✨", custom_id="shop_dialogue_browse")
    async def open_shop(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour de parler au marchand !", ephemeral=True)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description, required_role_id FROM shop_items WHERE shop_type = 'normal'")
        items = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title="🛒 Boutique Normale", color=discord.Color.gold())
        embed.description = "Voici les objets disponibles :"

        for name, price, desc, required_role_id in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        view = DynamicShopView(interaction.user, 'normal')
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ui.button(label="📖 Boutique Histoire", style=discord.ButtonStyle.success, emoji="📜", custom_id="shop_dialogue_story")
    async def open_story_shop(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)

        view = EpisodeShopView(interaction.user)
        current_ep = view.episode_num
        title = get_episode_title(current_ep)
        embed = discord.Embed(title=f"📜 {title}", color=discord.Color.dark_teal())
        embed.description = "Achète tous les objets de cet épisode !"

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type='episode' AND episode=?", (current_ep,))
        items = cursor.fetchall()
        conn.close()

        for name, price, desc in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def open_special_shop(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, price, description FROM shop_items WHERE shop_type = 'special'")
        items = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title="✨ Boutique Spéciale & Inédite", color=discord.Color.purple())
        embed.description = "Félicitations pour ton accès exclusif ! Voici les articles inédits :"

        for name, price, desc in items:
            embed.add_field(name=name, value=f"Prix : **{format_currency(price)}**\n*{desc}*", inline=False)

        view = DynamicShopView(interaction.user, 'special')
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ui.button(label="🎒 Voir mon inventaire", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="shop_dialogue_inventory")
    async def open_inventory(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour de parler au marchand !", ephemeral=True)

        user_id = interaction.user.id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT item_name, quantity FROM inventory WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        conn.close()

        embed = discord.Embed(title=f"🎒 Inventaire de {interaction.user.display_name}", color=discord.Color.blue())
        if not rows:
            embed.description = "Ton inventaire est désespérément vide..."
        else:
            description = [f"• **{item_name}** x`{qty}`" for item_name, qty in rows]
            embed.description = "\n".join(description)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="👋 Au revoir !", style=discord.ButtonStyle.danger, emoji="🚪", custom_id="shop_dialogue_leave")
    async def leave_chat(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas ton tour !", ephemeral=True)
        await interaction.response.defer()
        await interaction.delete_original_response()


class PersistentMerchantView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler au Marchand", style=discord.ButtonStyle.success, emoji="🦊", custom_id="persistent_merchant_talk_main")
    async def talk_to_merchant(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.user, discord.Member):
            return await interaction.followup.send("❌ Erreur.", ephemeral=True)

        embed = discord.Embed(
            title="🦊 Tom - Le Marchand Ambulant",
            description=(
                f"Oh, bonjour **{interaction.user.display_name}** ! "
                "Bienvenue dans ma boutique exclusive !\n\n"
                "*Qu'est-ce qui t'amène par ici aujourd'hui ? Fais ton choix camarade...*"
            ),
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f98a.png")

        view = ShopDialogueView(interaction.user)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class PersistentTroubadourView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler à Guillaume", style=discord.ButtonStyle.success, emoji="🪕", custom_id="persistent_troubadour_talk_main")
    async def talk_to_troubadour(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.user, discord.Member):
            return await interaction.followup.send("❌ Erreur.", ephemeral=True)

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

        page_indicator = ui.Button(label=f"Épisode {self.current_ep} / {TOTAL_EPISODES}", style=discord.ButtonStyle.blurple, disabled=True, row=0)
        self.add_item(page_indicator)

        next_btn = ui.Button(label="Suivant ▶️", style=discord.ButtonStyle.secondary, disabled=(self.current_ep >= TOTAL_EPISODES), row=0)
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

        has_story = EPISODES_LOADED and self.current_ep in EPISODE_STORIES and bool(EPISODE_STORIES[self.current_ep].strip())

        if not has_story:
            rest_btn = ui.Button(label="💤 Le troubadour se repose, revenez plus tard", style=discord.ButtonStyle.danger, disabled=True, row=1)
            self.add_item(rest_btn)
            return

        user_id = self.member.id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM story_progress WHERE user_id = ? AND episode_id = ?", (user_id, self.current_ep))
        is_unlocked = cursor.fetchone() is not None

        if self.current_ep == 1:
            has_all_items = True
        else:
            prev_ep = self.current_ep - 1
            cursor.execute("SELECT name FROM shop_items WHERE shop_type='episode' AND episode=?", (prev_ep,))
            prev_ep_items = [row[0] for row in cursor.fetchall()]

            has_all_items = False
            if prev_ep_items:
                cursor.execute("""
                    SELECT COUNT(*) FROM inventory 
                    WHERE user_id = ? AND item_name IN ({}) AND quantity > 0
                """.format(','.join(['?']*len(prev_ep_items))), [user_id] + prev_ep_items)
                owned_count = cursor.fetchone()[0]
                if owned_count >= len(prev_ep_items):
                    has_all_items = True
        conn.close()

        if is_unlocked:
            listen_btn = ui.Button(label="📖 Écouter / Relire l'histoire", style=discord.ButtonStyle.success, emoji="📜", row=1)
            listen_btn.callback = self.listen_callback
            self.add_item(listen_btn)
        elif self.current_ep == 1:
            listen_btn = ui.Button(label="📖 Écouter l'Histoire (Gratuit)", style=discord.ButtonStyle.success, emoji="📜", row=1)
            listen_btn.callback = self.listen_callback
            self.add_item(listen_btn)
        elif has_all_items:
            give_btn = ui.Button(label="🎁 Donner les reliques & Écouter", style=discord.ButtonStyle.primary, emoji="✨", row=1)
            give_btn.callback = self.give_callback
            self.add_item(give_btn)
        else:
            lock_btn = ui.Button(label="🔒 Épisode Verrouillé (Reliques précédentes manquantes)", style=discord.ButtonStyle.danger, disabled=True, row=1)
            self.add_item(lock_btn)

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
        if self.current_ep < TOTAL_EPISODES:
            self.current_ep += 1
            update_user_last_chapter(self.member.id, last_episode=self.current_ep)
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def listen_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)

        story_text = EPISODE_STORIES.get(self.current_ep, "« Une histoire mystérieuse... »")
        extra_txt = " (Offert à tous les voyageurs !)" if self.current_ep == 1 else " (Vous possédez déjà cet épisode dans vos archives permanentes.)"

        embed = discord.Embed(
            title=f"📜 Récit de {get_episode_title(self.current_ep)}",
            description=f"{story_text}\n\n*✨ {extra_txt}*",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def give_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)

        has_story = EPISODES_LOADED and self.current_ep in EPISODE_STORIES and bool(EPISODE_STORIES[self.current_ep].strip())
        if not has_story:
            return await interaction.response.send_message("❌ Le troubadour se repose, revenez plus tard !", ephemeral=True)

        user_id = self.member.id
        prev_ep = self.current_ep - 1

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM shop_items WHERE shop_type='episode' AND episode=?", (prev_ep,))
        prev_ep_items = [row[0] for row in cursor.fetchall()]

        cursor.execute("INSERT OR IGNORE INTO story_progress (user_id, episode_id) VALUES (?, ?)", (user_id, self.current_ep))

        for item in prev_ep_items:
            cursor.execute("DELETE FROM inventory WHERE user_id = ? AND item_name = ?", (user_id, item))
        conn.commit()
        conn.close()

        update_user_last_chapter(self.member.id, last_episode=self.current_ep)

        self.update_components()
        story_text = EPISODE_STORIES.get(self.current_ep, "« Une histoire mystérieuse... »")

        await send_public_log(
            content=f"📜 **{self.member.display_name}** a débloqué le chapitre **{self.current_ep}** de l'histoire de Guillaume le Troubadour !"
        )

        embed = discord.Embed(
            title=f"📜 Récit de {get_episode_title(self.current_ep)}",
            description=f"{story_text}\n\n*✨ (Guillaume a pris vos reliques de l'épisode {prev_ep} et a sauvegardé cet épisode à vie dans vos archives !) *",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")

        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        has_story = EPISODES_LOADED and self.current_ep in EPISODE_STORIES and bool(EPISODE_STORIES[self.current_ep].strip())

        if not has_story:
            status_txt = "💤 **[Le troubadour se repose, revenez plus tard]**"
        elif self.current_ep == 1:
            status_txt = "📖 **[Épisode Gratuit & Accessible]**"
        else:
            user_id = self.member.id
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM story_progress WHERE user_id = ? AND episode_id = ?", (user_id, self.current_ep))
            is_unlocked = cursor.fetchone() is not None
            conn.close()

            status_txt = "📖 **[Débloqué & Sauvegardé]**" if is_unlocked else f"🔒 **[Verrouillé / Reliques de l'épisode {self.current_ep - 1} requises]**"

        embed = discord.Embed(
            title=f"🪕 Guillaume le Troubadour — {get_episode_title(self.current_ep)}",
            description=(
                f"Statut : {status_txt}\n\n"
                "Utilisez les boutons ci-dessous pour naviguer entre les épisodes, donner vos reliques ou écouter les récits de votre choix !"
            ),
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url="https://images.emojiterra.com/google/android-10/512px/1f3ad.png")
        return embed


# ==========================================
# 11. COMMANDES ADMIN DE GESTION D'HISTOIRE / BOUTIQUE
# ==========================================

@bot.tree.command(name="reset-story", description="[Admin] Réinitialise la progression des histoires et supprime les reliques des inventaires")
@app_commands.checks.has_permissions(administrator=True)
async def reset_story(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM story_progress")
    cursor.execute("DELETE FROM inventory WHERE item_name LIKE '%Relique%'")
    cursor.execute("DELETE FROM user_last_chapter")
    conn.commit()
    conn.close()

    await interaction.followup.send("🔄 **Réinitialisation réussie !** Toutes les histoires validées, les reliques d'épisodes et les mémoires ont été remises à zéro.", ephemeral=True)


@bot.tree.command(name="inventory", description="Affiche ton inventaire d'achats")
async def inventory(interaction: discord.Interaction):
    user_id = interaction.user.id
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT item_name, quantity FROM inventory WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()

    embed = discord.Embed(title=f"🎒 Inventaire de {interaction.user.display_name}", color=discord.Color.blue())
    if not rows:
        embed.description = "Ton inventaire est désespérément vide..."
    else:
        description = [f"• **{item_name}** x`{qty}`" for item_name, qty in rows]
        embed.description = "\n".join(description)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="shop_add", description="[Admin] Ajoute un article normal, spécial ou épisode")
@app_commands.checks.has_permissions(administrator=True)
async def shop_add(
    interaction: discord.Interaction, 
    item_key: str, 
    name: str, 
    price: int, 
    description: str, 
    shop_type: str = "normal", 
    episode: int = 0,
    required_role: discord.Role = None, 
    role_to_give: discord.Role = None
):
    await interaction.response.defer(ephemeral=True)
    req_role_id = required_role.id if required_role else None
    give_role_id = role_to_give.id if role_to_give else None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO shop_items (item_key, name, price, description, shop_type, episode, required_role_id, role_to_give_id) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET 
            name = ?, price = ?, description = ?, shop_type = ?, episode = ?, required_role_id = ?, role_to_give_id = ?
    """, (item_key, name, price, description, shop_type, episode, req_role_id, give_role_id, 
          name, price, description, shop_type, episode, req_role_id, give_role_id))
    conn.commit()
    conn.close()

    ep_txt = f" (Épisode {episode})" if shop_type == "episode" else ""
    await interaction.followup.send(f"✅ L'article **{name}** a été ajouté au shop **{shop_type}**{ep_txt} avec succès !", ephemeral=True)


@bot.tree.command(name="shop_remove", description="[Admin] Supprime un article de la boutique")
@app_commands.checks.has_permissions(administrator=True)
async def shop_remove(interaction: discord.Interaction, item_key: str):
    await interaction.response.defer(ephemeral=True)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM shop_items WHERE item_key = ?", (item_key,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted > 0:
        await interaction.followup.send(f"✅ Article `{item_key}` supprimé.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Aucun article trouvé avec la clé `{item_key}`.", ephemeral=True)


async def refresh_public_quests():
    """Tâche qui tourne en arrière-plan pour rafraîchir les quêtes à minuit"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.datetime.now()
        midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time.min)
        seconds_until_midnight = (midnight - now).total_seconds()
        
        await asyncio.sleep(seconds_until_midnight)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, message_id FROM quest_channels")
        channels = cursor.fetchall()
        conn.close()
        
        new_quests = generate_public_quests()
        today = _today_str()
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO public_quests (quest_date, quests_json, generated_at) VALUES (?, ?, ?)",
            (today, json.dumps(new_quests), int(time.time()))
        )
        conn.commit()
        conn.close()
        
        for channel_id, message_id in channels:
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(message_id)
                    
                    embed = discord.Embed(
                        title=f"📋 Quêtes du Jour",
                        description="**8 quêtes sont à valider aujourd'hui !**\n\nTermine toutes les quêtes pour gagner ta récompense.\nClique sur le bouton ci-dessous pour suivre ta progression.",
                        color=discord.Color.gold()
                    )
                    
                    for i, q in enumerate(new_quests, 1):
                        embed.add_field(
                            name=f"{q['label']}",
                            value=f"{q['desc']}\n`⏳ À valider`",
                            inline=False
                        )
                    
                    embed.set_footer(text=f"Quêtes du {today} • Récompense : 500$")
                    
                    await msg.edit(embed=embed, view=PublicQuestsView())
                    print(f"✅ Quêtes mises à jour pour le {today}")
                except Exception as e:
                    print(f"❌ Erreur mise à jour du panneau de quêtes : {e}")


# ==========================================
# GESTION GLOBALE DES ERREURS DE COMMANDES SLASH
# ==========================================

async def _global_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    original_error = getattr(error, "original", error)

    print(
        f"❌ ERREUR COMMANDE SLASH | "
        f"Utilisateur={getattr(interaction.user, 'id', 'inconnu')} | "
        f"Commande={getattr(interaction.command, 'name', 'inconnue')} | "
        f"{type(original_error).__name__}: {original_error}"
    )
    traceback.print_exception(
        type(original_error),
        original_error,
        original_error.__traceback__,
    )

    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "❌ Tu n'as pas les permissions nécessaires pour utiliser cette commande."
    elif isinstance(error, app_commands.errors.CheckFailure):
        message = "❌ Tu n'es pas autorisé à utiliser cette commande."
    else:
        message = (
            f"❌ Une erreur est survenue avec cette commande : "
            f"`{type(original_error).__name__}`"
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as send_error:
        print(
            f"❌ Impossible d'envoyer le message d'erreur : "
            f"{type(send_error).__name__}: {send_error}"
        )

bot.tree.on_error = _global_app_command_error


# ==========================================
# INITIALISATION UNIQUE DU BOT
# ==========================================

@bot.event
async def on_ready():
    print(f"🤖 Bot connecté en tant que {bot.user} (ID: {bot.user.id}")
    
    await load_achievements_from_github()
    await load_episodes_from_github()

    try:
        init_db()
        print("💾 Base économique /data/economy.db : OK")
    except Exception as e:
        print(f"❌ ERREUR INIT BASE DE DONNÉES : {type(e).__name__}: {e}")
        traceback.print_exc()

    if not getattr(bot, "_persistent_views_registered", False):
        try:
            bot.add_view(PersistentMerchantView())
            bot.add_view(PersistentTroubadourView())
            bot.add_view(BobArenaView())
            bot.add_view(PublicQuestsView())
            bot._persistent_views_registered = True
            print("🦊 Vue persistante du Marchand : OK")
            print("🪕 Vue persistante de Guillaume : OK")
            print("📋 Vue persistante des Quêtes : OK")
        except Exception as e:
            print(f"❌ ERREUR VUES PERSISTANTES : {type(e).__name__}: {e}")
            traceback.print_exc()

    try:
        await asyncio.sleep(2)
        synced_global = await bot.tree.sync()
        print(f"🌲 {len(synced_global)} commandes slash synchronisées globalement.")

        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced_guild = await bot.tree.sync(guild=guild)
                print(
                    f"🏰 Serveur '{guild.name}' ({guild.id}) : "
                    f"{len(synced_guild)} commandes synchronisées."
                )
            except Exception as e:
                print(
                    f"❌ ERREUR SYNCHRO SERVEUR '{guild.name}' ({guild.id}) : "
                    f"{type(e).__name__}: {e}"
                )
                traceback.print_exc()
    except Exception as e:
        print(f"❌ ERREUR SYNCHRONISATION : {type(e).__name__}: {e}")
        traceback.print_exc()

    try:
        commands_list = [cmd.name for cmd in bot.tree.get_commands()]
        print(f"📋 Commandes disponibles : {', '.join(commands_list)}")
    except Exception as e:
        print(f"❌ ERREUR LISTE COMMANDES : {e}")

    bot.loop.create_task(refresh_public_quests())
    print("🔄 Tâche de rafraîchissement des quêtes démarrée")

    print("✅ Initialisation terminée : le bot est prêt à recevoir les commandes.")


# ==========================================
# 12. LANCEMENT DU BOT
# ==========================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Token Discord introuvable. Ajoute une variable d'environnement "
            "DISCORD_TOKEN (ou DISCORD_BOT_TOKEN/BOT_TOKEN/TOKEN) dans ton hébergeur. "
            "Ne mets pas le token directement dans le code."
        )
    bot.run(TOKEN)
