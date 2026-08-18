import asyncio
import datetime
import io
import os
import random
import time
import sqlite3
import traceback
import aiohttp
import json
import discord
from discord import app_commands, ui
from PIL import Image, ImageDraw, ImageFont, ImageOps
from discord.ext import commands

# ==========================================
# 1. CONFIGURATION INITIALE & CONSTANTES
# ==========================================

# Récupération sécurisée du token via la variable d'environnement
TOKEN = (
    os.getenv("TAVERNE_TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
)
MAX_BET = 500  # Mise maximale autorisée pour les jeux

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Cooldowns en mémoire : {(user_id, command_name): timestamp_expiration}
cooldowns = {}

# Mode test global pour les cooldowns
TEST_MODE_ENABLED = False


# ==========================================
# 2. GESTIONNAIRE D'ANIMATION DE MESSAGE
# ==========================================

class AnimatedMessageManager:
    def __init__(self, interaction: discord.Interaction, show_animation: bool = True):
        self.interaction = interaction
        self.show_animation = show_animation
        self.last_content = None
        self.last_embed = None

    async def update_animation(self, new_content: str = None, new_embed: discord.Embed = None, view: ui.View = None):
        if not self.show_animation:
            return

        if new_content != self.last_content or new_embed != self.last_embed:
            try:
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

        # --- TABLE POUR L'HISTOIRE (Guillaume le Troubadour) ---
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
    if amount > 0:
        update_quest_progress(user_id, "money_earned", amount)


def update_game_stats(user_id: int, won: bool):
    get_user(user_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if won:
            cursor.execute("UPDATE users SET games_played = COALESCE(games_played, 0) + 1, games_won = COALESCE(games_won, 0) + 1 WHERE user_id = ?", (user_id,))
        else:
            cursor.execute("UPDATE users SET games_played = COALESCE(games_played, 0) + 1, games_lost = COALESCE(games_lost, 0) + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    update_quest_progress(user_id, "games_played", 1)
    if won:
        update_quest_progress(user_id, "games_won", 1)


def format_currency(amount: int) -> str:
    return f"{amount:,} $".replace(",", " ")


init_db()


# ==========================================
# 3.1. SYSTÈMES DE QUÊTES JOURNALIÈRES
# ==========================================

QUEST_POOL = [
    {
        "key": "games_played",
        "label": "🎲 Joueur Assidu",
        "desc_tpl": "Jouer {target} partie(s) dans un jeu de casino (blackjack, dés, roulette, slots, PFC, poker...)",
        "target_range": (3, 6),
        "reward_range": (150, 300),
    },
    {
        "key": "games_won",
        "label": "🏆 Chanceux du Jour",
        "desc_tpl": "Gagner {target} partie(s) dans n'importe quel jeu",
        "target_range": (1, 3),
        "reward_range": (200, 400),
    },
    {
        "key": "work_done",
        "label": "💼 Travailleur",
        "desc_tpl": "Travailler {target} fois via /work",
        "target_range": (1, 3),
        "reward_range": (100, 250),
    },
    {
        "key": "arena_fight",
        "label": "⚔️ Guerrier de l'Arène",
        "desc_tpl": "Affronter Bob dans l'arène {target} fois",
        "target_range": (1, 2),
        "reward_range": (200, 400),
    },
    {
        "key": "duel_played",
        "label": "🤺 Duelliste",
        "desc_tpl": "Faire {target} duel(s) PvP contre un ami (taverne ou arène)",
        "target_range": (1, 2),
        "reward_range": (250, 450),
    },
    {
        "key": "bank_deposit",
        "label": "🏦 Épargnant",
        "desc_tpl": "Déposer de l'argent à la banque {target} fois",
        "target_range": (1, 3),
        "reward_range": (100, 200),
    },
    {
        "key": "pay_sent",
        "label": "💸 Généreux",
        "desc_tpl": "Envoyer de l'argent à un ami via /pay {target} fois",
        "target_range": (1, 2),
        "reward_range": (100, 200),
    },
    {
        "key": "crime_attempt",
        "label": "🥷 Petite Frappe",
        "desc_tpl": "Tenter ta chance chez John le Brigand {target} fois",
        "target_range": (1, 3),
        "reward_range": (150, 300),
    },
    {
        "key": "pmu_bet",
        "label": "🐎 Turfiste",
        "desc_tpl": "Parier sur une course chez Brook {target} fois",
        "target_range": (1, 3),
        "reward_range": (150, 300),
    },
    {
        "key": "vault_attempt",
        "label": "🔐 Braqueur de Coffre",
        "desc_tpl": "Tenter de braquer le coffre de la Brinks {target} fois",
        "target_range": (1, 2),
        "reward_range": (200, 400),
    },
    {
        "key": "money_earned",
        "label": "💰 Homme d'Affaires",
        "desc_tpl": "Gagner un total de {target} $ (jeux, travail, duels...)",
        "target_range": (500, 1500),
        "reward_range": (200, 400),
    },
    {
        "key": "beer_drunk",
        "label": "🍺 Bon Vivant",
        "desc_tpl": "Commander {target} pinte(s) chez Jim le Tavernier",
        "target_range": (1, 3),
        "reward_range": (100, 200),
    },
]


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


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

        chosen = random.sample(QUEST_POOL, k=min(5, len(QUEST_POOL)))
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

    return {
        "base_reward": base_reward,
        "multiplier": multiplier,
        "quest_streak": quest_streak,
        "total_reward": total_reward,
    }


# ==========================================
# 3.2. SYSTÈME DES ACHIEVEMENTS CHARGÉ DEPUIS GITHUB
# ==========================================

TIERS_NAMES = {
    1: "Bronze"
}

TIERS_COLORS = {
    1: "#CD7F32"  # Bronze
}

# Variables globales pour les succès chargés depuis GitHub
ACHIEVEMENTS_DEFS = {}
ACHIEVEMENTS_LOADED = False

# URL du fichier JSON sur GitHub
GITHUB_ACHIEVEMENTS_URL = "https://raw.githubusercontent.com/morgandede0-cyber/Bot-IV-Alpha/main/achievements_list.json"


async def load_achievements_from_github():
    """Charge la liste des succès depuis le fichier JSON sur GitHub"""
    global ACHIEVEMENTS_DEFS, ACHIEVEMENTS_LOADED
    
    # Fallback local MINIMAL en cas d'échec (seulement 1 succès pour tester)
    FALLBACK_ACHIEVEMENTS = {
        "games_master": {
            "title": "Maître des Jeux",
            "desc": "Gagner des parties dans les jeux de casino.",
            "thresholds": {"1": 1},
            "rewards": {"1": 200},
            "order": 1,
            "category": "Jeux",
            "tier": "Commun"
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


# Commande pour recharger les succès sans redémarrer le bot
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
            description="Les succès ont été chargés depuis le fallback local (GitHub inaccessible ou fichier invalide).",
            color=discord.Color.orange()
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)


# =============================================================
# CHARGEMENT DES ÉPISODES DEPUIS GITHUB (FICHIERS TEXTE)
# =============================================================

EPISODE_TITLES = {}
EPISODE_STORIES = {}
EPISODES_LOADED = False

# URL de base du dossier sur GitHub
EPISODES_BASE_URL = "https://raw.githubusercontent.com/morgandede0-cyber/Bot-IV-Alpha/main/episodes/"
# Nombre total d'épisodes disponibles
TOTAL_EPISODES = 30

async def load_episodes_from_github():
    """Charge les épisodes depuis les fichiers texte sur GitHub"""
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


# Commande admin pour recharger les épisodes
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
            description="Certains épisodes n'ont pas pu être chargés. Vérifie les fichiers dans le dossier `episodes/`.",
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


def evaluate_stat_for_achievement(key: str, user_id: int) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT wallet, bank, beers_today, games_played, games_won FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

        if not row:
            return 0

        wallet, bank, beers_today, games_played, games_won = row
        wallet = wallet or 0
        bank = bank or 0
        beers_today = beers_today or 0
        games_played = games_played or 0
        games_won = games_won or 0
        total_money = wallet + bank

        # === SUCCÈS QUÊTES ===
        if key.startswith("quest_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND claimed = 1", (user_id,))
            q_row = cursor.fetchone()
            return q_row[0] if q_row else 0
        
        # === SUCCÈS ARÈNE ===
        elif key.startswith("arena_"):
            if "essai" in key or "assidu" in key:
                return games_played
            elif "guerrier" in key or "champion" in key or "terreur" in key:
                return games_won
        
        # === SUCCÈS PMU ===
        elif key.startswith("pmu_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND quest_key = 'pmu_bet' AND claimed = 1", (user_id,))
            return cursor.fetchone()[0] if cursor.fetchone() else 0
        
        # === SUCCÈS CRIME ===
        elif key.startswith("crime_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND quest_key = 'crime_attempt' AND claimed = 1", (user_id,))
            return cursor.fetchone()[0] if cursor.fetchone() else 0
        
        # === SUCCÈS VAULT (BRINKS) ===
        elif key.startswith("vault_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND quest_key = 'vault_attempt' AND claimed = 1", (user_id,))
            return cursor.fetchone()[0] if cursor.fetchone() else 0
        
        # === SUCCÈS DUEL ===
        elif key.startswith("duel_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND quest_key = 'duel_played' AND claimed = 1", (user_id,))
            return cursor.fetchone()[0] if cursor.fetchone() else 0
        
        # === SUCCÈS DAILY ===
        elif key.startswith("daily_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND claimed = 1", (user_id,))
            return cursor.fetchone()[0] if cursor.fetchone() else 0
        
        # === SUCCÈS TAVERNE ===
        elif key.startswith("taverne_"):
            return beers_today
        
        # === SUCCÈS BANQUE ===
        elif key.startswith("bank_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND quest_key = 'bank_deposit' AND claimed = 1", (user_id,))
            return cursor.fetchone()[0] if cursor.fetchone() else 0
        
        # === SUCCÈS LARCIN ===
        elif key.startswith("larcin_"):
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND quest_key = 'crime_attempt' AND claimed = 1", (user_id,))
            return cursor.fetchone()[0] if cursor.fetchone() else 0
        
        # === ANCIENS SUCCÈS (Compatibilité) ===
        elif key == "games_master":
            return games_won
        elif key == "wealth_tycoon":
            return total_money
        elif key == "tavern_guest":
            return beers_today
        elif key == "arena_gladiator":
            return games_played
        elif key == "criminal_mind":
            return games_played
        elif key == "quest_seeker":
            cursor.execute("SELECT COUNT(*) FROM daily_quests WHERE user_id = ? AND claimed = 1", (user_id,))
            q_row = cursor.fetchone()
            return q_row[0] if q_row else 0
    
    return 0


async def check_and_unlock_achievements(user_id: int, bot_client=None) -> list:
    global ACHIEVEMENTS_DEFS, ACHIEVEMENTS_LOADED
    
    # Si les succès ne sont pas chargés, on les charge
    if not ACHIEVEMENTS_LOADED:
        await load_achievements_from_github()
    
    today = time.strftime("%Y-%m-%d")
    unlocked_now = []

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT achievement_key, tier FROM user_achievements WHERE user_id = ?", (user_id,))
        user_tiers = {row[0]: row[1] for row in cursor.fetchall()}

    for key, data in ACHIEVEMENTS_DEFS.items():
        if key in user_tiers:
            continue

        current_stat = evaluate_stat_for_achievement(key, user_id)
        
        if current_stat >= data["thresholds"]["1"]:
            target_tier = 1
            reward_sum = data["rewards"]["1"]
            update_wallet(user_id, reward_sum)

            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO user_achievements (user_id, achievement_key, tier, unlocked_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, achievement_key) DO UPDATE SET tier = ?, unlocked_at = ?",
                    (user_id, key, target_tier, today, target_tier, today)
                )
                conn.commit()

            unlocked_now.append({
                "key": key,
                "title": data["title"],
                "tier": target_tier,
                "tier_name": TIERS_NAMES.get(target_tier, "Bronze"),
                "reward": reward_sum
            })

            if bot_client:
                try:
                    with get_db_connection() as db_conn:
                        cur = db_conn.cursor()
                        cur.execute("SELECT channel_id FROM ai_channels WHERE ai_type = ?", ("achievements",))
                        ch_row = cur.fetchone()
                        if ch_row:
                            target_channel = bot_client.get_channel(ch_row[0])
                            if target_channel:
                                user_obj = bot_client.get_user(user_id)
                                user_mention = user_obj.mention if user_obj else f"<@{user_id}>"
                                
                                member_obj = target_channel.guild.get_member(user_id) if target_channel.guild else None
                                if member_obj:
                                    with get_db_connection() as db_conn2:
                                        cur2 = db_conn2.cursor()
                                        cur2.execute("SELECT achievement_key, tier FROM user_achievements WHERE user_id = ?", (user_id,))
                                        all_unlocked = {row[0]: row[1] for row in cur2.fetchall()}
                                    
                                    img_buf = await generate_mee6_profile_card(member_obj, all_unlocked)
                                    file = discord.File(fp=img_buf, filename="achievement.png")
                                    content = f"GG {user_mention}, tu as débloqué le succès **{data['title']}** ! 🎉"
                                    bot.loop.create_task(target_channel.send(content=content, file=file))
                except Exception as e:
                    print(f"❌ Erreur notification succès : {e}")

    return unlocked_now


def get_episode_title(ep_num: int) -> str:
    if EPISODES_LOADED and ep_num in EPISODE_TITLES:
        return EPISODE_TITLES.get(ep_num, f"Épisode {ep_num}")
    return f"Épisode {ep_num}"


# ==========================================
# 4. FONCTIONS UTILITAIRES & HELPERS
# ==========================================

def check_cooldown(user_id: int, command_name: str, duration: int) -> int:
    if TEST_MODE_ENABLED:
        return 0

    now = int(time.time())
    key = (user_id, command_name)
    expire = cooldowns.get(key, 0)
    if now < expire:
        return expire - now
    cooldowns[key] = now + duration
    return 0


def clear_cooldown(user_id: int, command_name: str = None):
    if command_name:
        cooldowns.pop((user_id, command_name), None)
    else:
        keys_to_remove = [k for k in cooldowns if k[0] == user_id]
        for k in keys_to_remove:
            cooldowns.pop(k, None)


async def validate_game_bet(
    interaction: discord.Interaction,
    command_name: str,
    bet: int,
    cooldown_sec: int = 3600,
) -> bool:
    if bet <= 0:
        await interaction.response.send_message(
            "❌ La mise doit être supérieure à 0 $ !", ephemeral=True
        )
        return False

    if bet > MAX_BET:
        await interaction.response.send_message(
            f"❌ La mise maximale autorisée est de **{MAX_BET} $** !", ephemeral=True
        )
        return False

    wallet, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
    if wallet < bet:
        await interaction.response.send_message(
            "❌ Solde insuffisant dans ton portefeuille ! Pense à retirer de"
            " l'argent via /banque.",
            ephemeral=True,
        )
        return False

    retry_after = check_cooldown(interaction.user.id, command_name, cooldown_sec)
    if retry_after > 0:
        minutes, seconds = divmod(retry_after, 60)
        await interaction.response.send_message(
            f"⏳ Tu dois attendre **{minutes} min et {seconds} sec** avant de pouvoir"
            " rejouer.",
            ephemeral=True,
        )
        return False

    return True


# ==========================================
# 5. MODALES DE MISE POUR CHAQUE JEU
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

        await self.callback_game(interaction, bet_amount)


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

    def __init__(self, horse_choice: int, dynamic_odds: dict):
        super().__init__()
        self.horse_choice = horse_choice
        self.dynamic_odds = dynamic_odds

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            bet_amount = int(self.bet_input.value)
        except ValueError:
            return await interaction.followup.send("❌ Veuillez entrer un montant valide.", ephemeral=True)

        await run_brook_pmu_game(interaction, self.horse_choice, bet_amount, self.dynamic_odds)


# ==========================================
# 5.1. SYSTÈMES DE DUEL ENTRE JOUEURS (JIM)
# ==========================================

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
            else:
                update_wallet(self.opponent.id, self.bet)
                update_wallet(self.challenger.id, -self.bet)
                update_game_stats(self.opponent.id, won=True)
                update_game_stats(self.challenger.id, won=False)
                await check_and_unlock_achievements(self.opponent.id, bot_client=bot)
                res_text = f"🏆 **Victoire de {self.opponent.mention} !** Il remporte **{format_currency(self.bet)}**."

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
                update_game_stats(self.challenger.id, won=False)
                await check_and_unlock_achievements(self.challenger.id, bot_client=bot)
                res_text = f"🏆 **Victoire de {self.challenger.mention} ({c_score} vs {o_score}) !** Il remporte **{format_currency(self.bet)}**."
            else:
                update_wallet(self.opponent.id, self.bet)
                update_wallet(self.challenger.id, -self.bet)
                update_game_stats(self.opponent.id, won=True)
                update_game_stats(self.opponent.id, won=False)
                await check_and_unlock_achievements(self.opponent.id, bot_client=bot)
                res_text = f"🏆 **Victoire de {self.opponent.mention} ({o_score} vs {c_score}) !** Il remporte **{format_currency(self.bet)}**."

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
    def __init__(self, challenger: discord.Member, opponent: discord.Member, game_type: str, bet: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.game_type = game_type
        self.bet = bet

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Ce n'est pas à vous d'accepter ce duel !", ephemeral=True)
            return False
        return True

    @ui.button(label="Accepter le Duel", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        wallet_opp, _, _, _, _, _, _, _, _ = get_user(self.opponent.id)
        wallet_chal, _, _, _, _, _, _, _, _ = get_user(self.challenger.id)

        if wallet_opp < self.bet:
            return await interaction.response.send_message("❌ Vous n'avez pas assez d'argent dans votre portefeuille pour accepter ce duel.", ephemeral=True)
        if wallet_chal < self.bet:
            return await interaction.response.send_message(f"❌ {self.challenger.mention} n'a plus assez d'argent pour honorer le duel.", ephemeral=True)

        for child in self.children:
            child.disabled = True

        update_quest_progress(self.challenger.id, "duel_played", 1)
        update_quest_progress(self.opponent.id, "duel_played", 1)

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
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(
            title="⚔️ Duel Refusé",
            description=f"{self.opponent.mention} a décliné le duel proposé par {self.challenger.mention}.",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=self)


# ==========================================
# 6. INTERFACES INTERACTIVES & MODALES (BANQUE & DAB)
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

            update_quest_progress(interaction.user.id, "bank_deposit", 1)
            await check_and_unlock_achievements(interaction.user.id, bot_client=bot)

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


# ==========================================
# 7. INTERFACES DES IA : JIM, JOHN, BROOK & BOB
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

        wallet_chal, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
        if wallet_chal < bet:
            return await interaction.followup.send("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)

        wallet_opp, _, _, _, _, _, _, _, _ = get_user(self.opponent.id)
        if wallet_opp < bet:
            return await interaction.followup.send(f"❌ {self.opponent.mention} n'a pas assez d'argent dans son portefeuille pour accepter cette mise.", ephemeral=True)

        game_name = "Dés du Destin" if self.game_type == "dice" else "Pierre-Feuille-Ciseaux"
        view = DuelAcceptView(interaction.user, self.opponent, self.game_type, bet)

        embed = discord.Embed(
            title="⚔️ DÉFI DE DUEL (TAVERNE)",
            description=(
                f"{interaction.user.mention} défie {self.opponent.mention} à un duel de **{game_name}** sous l'œil de Jim !\n\n"
                f"💰 **Mise en jeu** : `{format_currency(bet)}` par joueur\n\n"
                f"{self.opponent.mention}, acceptez-vous ce défi ?"
            ),
            color=discord.Color.dark_red()
        )
        await interaction.followup.send(content=self.opponent.mention, embed=embed, view=view)


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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET beers_today = ?, last_beer_date = ? WHERE user_id = ?",
                (beers_today, last_beer_date, user_id)
            )
            conn.commit()

        await check_and_unlock_achievements(user_id, bot_client=bot)

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
        retry_after = check_cooldown(user_id, "john_rob", 3600)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            msg_text = f'🗡️ *John* : "Calme tes ardeurs de voleur, attends **{minutes}m {seconds}s**."'
            return await interaction.followup.send(msg_text, ephemeral=True)

        victim = self.values[0]
        if victim.id == user_id:
            return await interaction.followup.send("❌ Tu ne peux pas te voler toi-même.", ephemeral=True)

        victim_wallet, _, _, _, _, _, _, _, _ = get_user(victim.id)
        thief_wallet, _, _, _, _, _, _, _, _ = get_user(user_id)

        if victim_wallet < 50:
            return await interaction.followup.send(f"🗡️ *John* : \" {victim.mention} n'a pas un sou, c'est une perte de temps.\"", ephemeral=True)

        if random.random() < 0.4:
            stolen = random.randint(10, int(victim_wallet * 0.5))
            update_wallet(victim.id, -stolen)
            update_wallet(user_id, stolen)
            await check_and_unlock_achievements(user_id, bot_client=bot)
            await interaction.followup.send(f"🥷 **[JOHN LE BRIGAND]** Bien joué ! Tu as volé **{format_currency(stolen)}** à {victim.mention} !", ephemeral=True)
        else:
            fine = min(200, thief_wallet)
            if fine > 0:
                update_wallet(user_id, -fine)
            await interaction.followup.send(f"❌ **[JOHN LE BRIGAND]** Échec critique ! John t'a pris une commission de fuite de **{format_currency(fine)}**.", ephemeral=True)


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
            await check_and_unlock_achievements(user_id, bot_client=bot)
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
            _, bank, _, _, _, _, _, _, _ = get_user(user_id)
            fine = int(bank * 0.05)
            if fine > 0:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE users SET bank = bank - ? WHERE user_id = ?", (fine, user_id))
                    conn.commit()

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
        retry_after = check_cooldown(user_id, "john_crime", 1800)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            msg_text = f'🥷 *John* : "Reviens dans **{minutes}m {seconds}s**."'
            return await interaction.followup.send(msg_text, ephemeral=True)

        success = random.choice([True, False])
        wallet, _, _, _, _, _, _, _, _ = get_user(user_id)
        update_quest_progress(user_id, "crime_attempt", 1)

        if success:
            gain = random.randint(300, 1000)
            update_wallet(user_id, gain)
            await check_and_unlock_achievements(user_id, bot_client=bot)
            await interaction.followup.send(f"🥷 **[JOHN LE BRIGAND]** Joli coup ! Vol réussi. +**{format_currency(gain)}**", ephemeral=True)
        else:
            loss = min(random.randint(100, 400), wallet)
            if loss > 0:
                update_wallet(user_id, -loss)
                await interaction.followup.send(f"🚨 **[JOHN LE BRIGAND]** La milice t'a repéré ! Amende : -**{format_currency(loss)}**", ephemeral=True)
            else:
                await interaction.followup.send("🚨 **[JOHN LE BRIGAND]** Pris la main dans le sac, mais tu es trop pauvre pour payer.", ephemeral=True)

    @ui.button(label="Braquer quelqu'un", style=discord.ButtonStyle.secondary, emoji="🗡️", custom_id="john_rob_btn")
    async def rob_btn(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        retry_after = check_cooldown(user_id, "john_rob", 3600)
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
        retry_after = check_cooldown(user_id, "john_vault", 7200)
        if retry_after > 0:
            hours, remainder = divmod(retry_after, 3600)
            minutes, seconds = divmod(remainder, 60)
            return await interaction.response.send_message(f'🔐 *John* : "Le convoi de la Brinks est surveillé. Attends **{hours}h {minutes}m {seconds}s** avant de replonger."', ephemeral=True)

        update_quest_progress(user_id, "vault_attempt", 1)
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
# 7.1. BOB LES MAITRE D'ARME (ARÈNE DE COMBAT)
# ==========================================

class ArenaFightView(ui.View):
    def __init__(self, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.bet = bet
        self.player_hp = 100
        self.bob_hp = 100

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ce n'est pas votre combat !", ephemeral=True)
            return False
        return True

    def build_embed(self, status_msg: str, color=discord.Color.red()) -> discord.Embed:
        desc = (
            f"⚔️ **DUEL DANS L'ARÈNE** ⚔️\n\n"
            f"👤 **Votre PV** : `{'❤️' * int(self.player_hp / 10)}` ({self.player_hp}/100)\n"
            f"🛡️ **Bob le Maître d'Arme** : `{'🖤' * int(self.bob_hp / 10)}` ({self.bob_hp}/100)\n\n"
            f"📜 *Action* : {status_msg}"
        )
        embed = discord.Embed(title="🏟️ Arène des IV Sceaux", description=desc, color=color)
        embed.set_footer(text=f"Mise en jeu : {format_currency(self.bet)}")
        return embed

    async def process_turn(self, interaction: discord.Interaction, player_move: str):
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
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            embed = self.build_embed(f"{p_text}\n\n🏆 **VICTOIRE !** Bob s'agenouille, vaincu par votre bravoure ! +**{format_currency(gain)}**", color=discord.Color.green())
            return await interaction.response.edit_message(embed=embed, view=self)

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
            embed = self.build_embed(f"{p_text}\n{b_text}\n\n💀 **DÉFAITE !** Bob vous terrasse d'un ultime coup de taille. -**{format_currency(self.bet)}**", color=discord.Color.dark_red())
            return await interaction.response.edit_message(embed=embed, view=self)

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
    view = ArenaFightView(interaction.user.id, bet)
    embed = view.build_embed("Le combat commence ! Choisissez votre style d'attaque.")
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class BobArenaView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Entrer dans l'Arène (Combattre Bob)", style=discord.ButtonStyle.danger, emoji="⚔️", custom_id="bob_arena_fight")
    async def fight_btn(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        retry_after = check_cooldown(user_id, "arene_fight", 1800)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            return await interaction.response.send_message(f'⚔️ *Bob* : "Reprenez votre souffle, l\'arène n\'ouvre ses portes qu\'dans **{minutes}m {seconds}s**."', ephemeral=True)

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

        wallet_chal, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
        if wallet_chal < bet:
            return await interaction.followup.send("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)

        wallet_opp, _, _, _, _, _, _, _, _ = get_user(self.opponent.id)
        if wallet_opp < bet:
            return await interaction.followup.send(f"❌ {self.opponent.mention} n'a pas assez d'argent dans son portefeuille pour accepter ce duel.", ephemeral=True)

        view = ArenaDuelAcceptView(interaction.user, self.opponent, bet)
        embed = discord.Embed(
            title="⚔️ DÉFI DE L'ARÈNE",
            description=(
                f"{interaction.user.mention} défie {self.opponent.mention} en duel dans l'arène de Bob !\n\n"
                f"💰 **Mise en jeu** : `{format_currency(bet)}` par joueur\n\n"
                f"{self.opponent.mention}, acceptez-vous ce combat ?"
            ),
            color=discord.Color.dark_gold()
        )
        await interaction.followup.send(content=self.opponent.mention, embed=embed, view=view)


class ArenaDuelAcceptView(ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Ce n'est pas à vous d'accepter ce duel !", ephemeral=True)
            return False
        return True

    @ui.button(label="Accepter le Duel", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        wallet_opp, _, _, _, _, _, _, _, _ = get_user(self.opponent.id)
        wallet_chal, _, _, _, _, _, _, _, _ = get_user(self.challenger.id)

        if wallet_opp < self.bet:
            return await interaction.response.send_message("❌ Vous n'avez pas assez d'argent dans votre portefeuille pour accepter ce duel.", ephemeral=True)
        if wallet_chal < self.bet:
            return await interaction.response.send_message(f"❌ {self.challenger.mention} n'a plus assez d'argent pour honorer le duel.", ephemeral=True)

        for child in self.children:
            child.disabled = True

        update_quest_progress(self.challenger.id, "duel_played", 1)
        update_quest_progress(self.opponent.id, "duel_played", 1)

        view = ArenaPvPView(self.challenger, self.opponent, self.bet)
        embed = view.build_embed("⚔️ Le duel commence ! Chaque combattant doit choisir son action en secret.")
        await interaction.response.edit_message(embed=embed, view=view)

    @ui.button(label="Refuser", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(
            title="⚔️ Duel Refusé",
            description=f"{self.opponent.mention} a décliné le duel proposé par {self.challenger.mention}.",
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=self)


class ArenaPvPView(ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
        super().__init__(timeout=120)
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet
        self.hp = {challenger.id: 100, opponent.id: 100}
        self.moves = {}
        self.round_num = 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.challenger.id, self.opponent.id):
            await interaction.response.send_message("❌ Ce duel ne vous concerne pas !", ephemeral=True)
            return False
        if interaction.user.id in self.moves:
            await interaction.response.send_message("❌ Vous avez déjà choisi votre action pour ce round !", ephemeral=True)
            return False
        return True

    def build_embed(self, status_msg: str, color=discord.Color.dark_gold()) -> discord.Embed:
        desc = (
            f"⚔️ **DUEL DE L'ARÈNE — Round {self.round_num}** ⚔️\n\n"
            f"👤 **{self.challenger.display_name}** : `{'❤️' * max(0, int(self.hp[self.challenger.id] / 10))}` ({self.hp[self.challenger.id]}/100)\n"
            f"👤 **{self.opponent.display_name}** : `{'❤️' * max(0, int(self.hp[self.opponent.id] / 10))}` ({self.hp[self.opponent.id]}/100)\n\n"
            f"📜 *Déroulement* : {status_msg}"
        )
        embed = discord.Embed(title="🏟️ Arène des Sceaux - Duel PvP", description=desc, color=color)
        embed.set_footer(text=f"Mise en jeu : {format_currency(self.bet)}")
        return embed

    def _resolve_damage(self, attacker_move: str, defender_move: str) -> int:
        if attacker_move == "heavy":
            dmg = random.randint(18, 32) if random.random() < 0.6 else 0
        elif attacker_move == "fast":
            dmg = random.randint(10, 18)
        else:
            dmg = 0

        if defender_move == "parry":
            if attacker_move == "heavy":
                dmg = 0
            elif attacker_move == "fast":
                dmg = dmg // 2
        return dmg

    async def process_choice(self, interaction: discord.Interaction, move: str):
        self.moves[interaction.user.id] = move
        emojis = {"heavy": "🪓 Frappe Lourde", "fast": "🗡️ Estoc Rapide", "parry": "🛡️ Posture Défensive"}
        await interaction.response.send_message(f"🔒 Action enregistrée : **{emojis[move]}**.", ephemeral=True)

        if len(self.moves) < 2:
            return

        chal_move = self.moves[self.challenger.id]
        opp_move = self.moves[self.opponent.id]
        emojis_map = {"heavy": "🪓 Frappe Lourde", "fast": "🗡️ Estoc Rapide", "parry": "🛡️ Posture Défensive"}

        dmg_to_opponent = self._resolve_damage(chal_move, opp_move)
        dmg_to_challenger = self._resolve_damage(opp_move, chal_move)

        self.hp[self.opponent.id] = max(0, self.hp[self.opponent.id] - dmg_to_opponent)
        self.hp[self.challenger.id] = max(0, self.hp[self.challenger.id] - dmg_to_challenger)

        round_text = (
            f"{self.challenger.mention} choisit `{emojis_map[chal_move]}` (-{dmg_to_opponent} PV à l'adversaire)\n"
            f"{self.opponent.mention} choisit `{emojis_map[opp_move]}` (-{dmg_to_challenger} PV à l'adversaire)"
        )

        chal_dead = self.hp[self.challenger.id] <= 0
        opp_dead = self.hp[self.opponent.id] <= 0

        if chal_dead or opp_dead:
            for child in self.children:
                child.disabled = True

            if chal_dead and opp_dead:
                res_text = "\n\n🤝 **ÉGALITÉ SANGLANTE !** Les deux combattants s'effondrent, les mises sont remboursées."
                color = discord.Color.greyple()
            elif opp_dead:
                update_wallet(self.challenger.id, self.bet)
                update_wallet(self.opponent.id, -self.bet)
                update_game_stats(self.challenger.id, won=True)
                update_game_stats(self.opponent.id, won=False)
                await check_and_unlock_achievements(self.challenger.id, bot_client=bot)
                res_text = f"\n\n🏆 **VICTOIRE de {self.challenger.mention} !** Il remporte **{format_currency(self.bet)}**."
                color = discord.Color.green()
            else:
                update_wallet(self.opponent.id, self.bet)
                update_wallet(self.challenger.id, -self.bet)
                update_game_stats(self.opponent.id, won=True)
                update_game_stats(self.opponent.id, won=False)
                await check_and_unlock_achievements(self.opponent.id, bot_client=bot)
                res_text = f"\n\n🏆 **VICTOIRE de {self.opponent.mention} !** Il remporte **{format_currency(self.bet)}**."
                color = discord.Color.green()

            embed = self.build_embed(round_text + res_text, color=color)
            return await interaction.message.edit(embed=embed, view=self)

        self.moves = {}
        self.round_num += 1
        embed = self.build_embed(round_text + "\n\n➡️ Round suivant : choisissez votre prochaine action !")
        await interaction.message.edit(embed=embed, view=self)

    @ui.button(label="Frappe Lourde (60%)", style=discord.ButtonStyle.danger, emoji="🪓")
    async def heavy_strike(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_choice(interaction, "heavy")

    @ui.button(label="Estoc Rapide (100%)", style=discord.ButtonStyle.primary, emoji="🗡️")
    async def fast_strike(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_choice(interaction, "fast")

    @ui.button(label="Posture Défensive", style=discord.ButtonStyle.secondary, emoji="🛡️")
    async def parry_stance(self, interaction: discord.Interaction, button: ui.Button):
        await self.process_choice(interaction, "parry")


PMU_ODDS = {1: 3.0, 2: 3.0, 3: 3.0, 4: 3.0}


async def run_pmu_game(interaction: discord.Interaction, cheval: int, bet: int):
    if not await validate_game_bet(interaction, "pmu", bet, cooldown_sec=900):
        return

    update_quest_progress(interaction.user.id, "pmu_bet", 1)
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
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        res_msg = f"🏆 **[PMU] VICTOIRE !** #{gagnant} ({chevaux[gagnant]['nom']}) a gagné ! Ton pari sur **{chevaux[cheval]['nom']}** (cote x{cote}) passe haut la main ! +**{format_currency(gain)}**"
    else:
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        res_msg = f"❌ **[PMU] PERDU !** C'est #{gagnant} ({chevaux[gagnant]['nom']}) qui a gagné. Ton pari sur **{chevaux[cheval]['nom']}** (cote x{cote}) est perdant. -**{format_currency(bet)}**"

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


async def run_brook_pmu_game(interaction: discord.Interaction, horse_choice: int, bet: int, dynamic_odds: dict):
    if not await validate_game_bet(interaction, "brook_bet", bet, cooldown_sec=1800):
        return

    update_quest_progress(interaction.user.id, "pmu_bet", 1)
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

    await interaction.followup.send(initial_piste, ephemeral=True)
    anim_manager = AnimatedMessageManager(interaction, show_animation=show_anim)

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
        await anim_manager.update_animation(new_content=piste_str)

    max_p = max(positions.values())
    gagnants = [c for c, p in positions.items() if p >= max_p]

    gagnant = random.choices(gagnants, weights=[weights[c-1] for c in gagnants], k=1)[0] if len(gagnants) > 1 else gagnants[0]

    cote = dynamic_odds[horse_choice]

    if horse_choice == gagnant:
        gain = int(bet * cote)
        update_wallet(interaction.user.id, gain - bet)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
        res_msg = f"🏆 **[BROOK LA BOOKMAKEUSE] VICTOIRE !** #{gagnant} ({chevaux[gagnant]['nom']}) a gagné ! Ton pari sur **{chevaux[horse_choice]['nom']}** (cote x{cote}) passe haut la main ! +**{format_currency(gain)}**"
    else:
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)
        res_msg = f"❌ **[BROOK LA BOOKMAKEUSE] PERDU !** C'est #{gagnant} ({chevaux[gagnant]['nom']}) qui a gagné. Ton pari sur **{chevaux[horse_choice]['nom']}** (cote x{cote}) est perdant. -**{format_currency(bet)}**"

    final_piste = f"🏁 **Brook - Arrivée de la course PMU !**\n```text\n┌── HIPPODROME ────────┐\n"
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

    new_odds = generate_brook_odds()
    file_brook = discord.File("assets/brook.png", filename="brook.png") if os.path.exists("assets/brook.png") else None
    new_embed = discord.Embed(
        description=(
            "📜 **Guichet des Paris — BROOK**\n"
            f"*(Tient un carnet de notes rempli de chiffres)* Bienvenue chez **Brook** ! Les cotes ont varié pour ce tour. "
            f"Clique sur ton cheval favori pour lancer la course animée : Canabis (x{new_odds[1]}), Jolly Jumper (x{new_odds[2]}), Pégase (x{new_odds[3]}) ou Petit Tonnerre (x{new_odds[4]})."
        ),
        color=0x1ABC9C
    )
    if file_brook:
        new_embed.set_image(url="attachment://brook.png")

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT channel_id FROM ai_channels WHERE ai_type = ?", ("brook",))
            row = cur.fetchone()
            if row:
                channel = bot.get_channel(row[0])
                if channel:
                    async for message in channel.history(limit=10):
                        if message.author == bot.user and message.embeds and "Brook" in message.embeds[0].description:
                            kwargs = {"embed": new_embed, "view": BrookBookmakerView(new_odds)}
                            if file_brook:
                                kwargs["files"] = [file_brook]
                            await message.edit(**kwargs)
                            break
    except Exception as e:
        print(f"❌ Erreur lors de l'actualisation du panneau Brook : {e}")


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
        await interaction.response.send_modal(BrookPMUBetModal(1, self.odds))

    @ui.button(label="Jolly Jumper", style=discord.ButtonStyle.primary, emoji="🐴", custom_id="brook_horse_2")
    async def horse_2(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrookPMUBetModal(2, self.odds))

    @ui.button(label="Pégase", style=discord.ButtonStyle.primary, emoji="🦄", custom_id="brook_horse_3")
    async def horse_3(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrookPMUBetModal(3, self.odds))

    @ui.button(label="Petit Tonnerre", style=discord.ButtonStyle.primary, emoji="🏇", custom_id="brook_horse_4")
    async def horse_4(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BrookPMUBetModal(4, self.odds))


# ==========================================
# 8. COMMANDES D'ÉCONOMIE, BANQUE & ADMIN
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

    wallet_chal, _, _, _, _, _, _, _, _ = get_user(interaction.user.id)
    if wallet_chal < bet:
        return await interaction.followup.send("❌ Solde insuffisant dans votre portefeuille !", ephemeral=True)

    wallet_opp, _, _, _, _, _, _, _, _ = get_user(opponent.id)
    if wallet_opp < bet:
        return await interaction.followup.send(f"❌ {opponent.mention} n'a pas assez d'argent dans son portefeuille pour accepter cette mise.", ephemeral=True)

    game_name = "Dés du Destin" if game == "dice" else "Pierre-Feuille-Ciseaux"
    view = DuelAcceptView(interaction.user, opponent, game, bet)

    embed = discord.Embed(
        title="⚔️ DÉFI DE DUEL",
        description=(
            f"{interaction.user.mention} défie {opponent.mention} à un duel de **{game_name}** !\n\n"
            f"💰 **Mise en jeu** : `{format_currency(bet)}` par joueur\n\n"
            f"{opponent.mention}, acceptez-vous ce défi ?"
        ),
        color=discord.Color.dark_red()
    )
    await interaction.followup.send(content=opponent.mention, embed=embed, view=view)


@bot.tree.command(name="pmu", description="Parie sur une course de chevaux rapide (PMU)")
async def pmu(interaction: discord.Interaction):
    await interaction.response.send_modal(PMUBetModal())


@bot.tree.command(name="vault", description="Tenter de braquer le coffre de la Brinks")
async def vault(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    retry_after = check_cooldown(user_id, "john_vault", 7200)
    if retry_after > 0:
        hours, remainder = divmod(retry_after, 3600)
        minutes, seconds = divmod(remainder, 60)
        return await interaction.followup.send(f'🔐 *John* : "Le convoi de la Brinks est surveillé. Attends **{hours}h {minutes}m {seconds}s** avant de replonger."', ephemeral=True)

    update_quest_progress(user_id, "vault_attempt", 1)
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


@bot.tree.command(name="richest", description="Affiche l'économie globale du serveur et le classement exact")
async def richest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
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


@bot.tree.command(name="reset-achievements", description="[ADMIN] Réinitialise définitivement les succès d'un joueur ou de tout le monde")
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
    await check_and_unlock_achievements(user_id, bot_client=bot)

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
    await interaction.followup.send(random.choice(jobs))


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
    await interaction.followup.send(f"💸 {interaction.user.mention} ➔ **{format_currency(amount)}** à {receiver.mention} !")


@bot.tree.command(name="setup", description="[ADMIN] Configure les salons pour la Banque, Jim, John, Brook, Bob, Marchand, Troubadour ou les Succès")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(ai_type=[
    app_commands.Choice(name="Tous les PNJ dans un salon unique (Carrefour PNJ)", value="all"),
    app_commands.Choice(name="banque (DAB)", value="banque"),
    app_commands.Choice(name="taverne (Jim)", value="taverne"),
    app_commands.Choice(name="crime (John)", value="crime"),
    app_commands.Choice(name="bookmaker (Brook)", value="brook"),
    app_commands.Choice(name="arene (Bob le maître d'arme)", value="arene"),
    app_commands.Choice(name="marchand (Tom)", value="marchand"),
    app_commands.Choice(name="troubadour (Guillaume)", value="troubadour"),
    app_commands.Choice(name="achievements (Salon des succès débloqués)", value="achievements")
])
async def setup(interaction: discord.Interaction, ai_type: str, salon: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id

    if ai_type == "all":
        await interaction.followup.send(f"✅ Le Carrefour des PNJ a bien été déployé dans {salon.mention} !", ephemeral=True)

        # 1. Jim
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

        # 2. John
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

        # 3. Bob
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

        # 4. Brook
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

        # 5. Marchand (Tom)
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

        # 6. Troubadour (Guillaume)
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

    wallet, bank, _, _, _, _, _, _, _ = get_user(membre.id)
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
    clear_cooldown(membre.id)
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
# 9. JEUX DE CASINO & LOGIQUES DE LANCEMENT
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
            await check_and_unlock_achievements(self.game.user_id, bot_client=bot)
            embed = self.build_embed(game_over=True, result_message=f"🎉 21 ! +{format_currency(gain)}")
            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()
        elif player_score > 21:
            for child in self.children:
                child.disabled = True
            update_wallet(self.game.user_id, -self.game.bet)
            update_game_stats(self.game.user_id, won=False)
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
            await check_and_unlock_achievements(self.game.user_id, bot_client=bot)
            res = f"🎉 Banque > 21 ! +{format_currency(gain)}"
        elif player_score > dealer_score:
            gain = self.game.bet
            update_wallet(self.game.user_id, gain)
            update_game_stats(self.game.user_id, won=True)
            await check_and_unlock_achievements(self.game.user_id, bot_client=bot)
            res = f"🎉 Gagné ! +{format_currency(gain)}"
        elif player_score < dealer_score:
            update_wallet(self.game.user_id, -self.game.bet)
            update_game_stats(self.game.user_id, won=False)
            res = "❌ Perdu !"
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
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
    elif f1 == f2 or f2 == f3 or f1 == f3:
        reward = int(bet * 1.5)
        status = f"DUO! +{format_currency(reward)}"
        update_wallet(interaction.user.id, reward - bet)
        update_game_stats(interaction.user.id, won=True)
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
    else:
        status = f"PERDU! -{format_currency(bet)}"
        update_wallet(interaction.user.id, -bet)
        update_game_stats(interaction.user.id, won=False)

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
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            status = f"VICTOIRE! +{format_currency(self.bet)}"
        elif (p1 + p2) < (d1 + d2):
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            status = f"PERDU! -{format_currency(self.bet)}"
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
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            status = f"GAGNÉ! +{format_currency(reward)}"
        else:
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            status = f"PERDU! -{format_currency(self.bet)}"

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
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
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
        await check_and_unlock_achievements(self.user_id, bot_client=bot)
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
            await check_and_unlock_achievements(self.user_id, bot_client=bot)
            res = "🎉 Gagné !"
            face = "(^o^) 🏆"
        else:
            update_wallet(self.user_id, -self.bet)
            update_game_stats(self.user_id, won=False)
            res = "❌ Perdu !"
            face = "(T_T) 💀"

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
        await check_and_unlock_achievements(interaction.user.id, bot_client=bot)
    else:
        update_wallet(interaction.user.id, -mise)
        update_game_stats(interaction.user.id, won=False)

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

class PersistentTroubadourView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Parler à Guillaume", style=discord.ButtonStyle.success, emoji="🪕", custom_id="persistent_troubadour_talk_main")
    async def talk_to_troubadour(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.user, discord.Member):
            return await interaction.followup.send("❌ Erreur.", ephemeral=True)

        view = TroubadourPaginationView(interaction.user, current_ep=1)
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
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("❌ Ce n'est pas votre tour !", ephemeral=True)
        if self.current_ep < TOTAL_EPISODES:
            self.current_ep += 1
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

        self.update_components()
        story_text = EPISODE_STORIES.get(self.current_ep, "« Une histoire mystérieuse... »")

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


# --- VUE POUR LA BOUTIQUE DES ÉPISODES ---
class EpisodeShopView(ui.View):
    def __init__(self, member: discord.Member, episode_num: int):
        super().__init__(timeout=120)
        self.member = member
        self.episode_num = episode_num
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


# --- VUE POUR LA BOUTIQUE DYNAMIQUE CLASSIQUE ---
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


# --- MENU DE DIALOGUE PRINCIPAL DU MARCHAND ---
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

        current_ep = 1
        view = EpisodeShopView(interaction.user, current_ep)
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


# ==========================================
# 8. COMMANDES ADMIN DE GESTION D'HISTOIRE / BOUTIQUE
# ==========================================

@bot.tree.command(name="reset-story", description="[Admin] Réinitialise la progression des histoires et supprime les reliques des inventaires")
@app_commands.checks.has_permissions(administrator=True)
async def reset_story(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM story_progress")
    cursor.execute("DELETE FROM inventory WHERE item_name LIKE '%Relique%'")
    conn.commit()
    conn.close()

    await interaction.followup.send("🔄 **Réinitialisation réussie !** Toutes les histoires validées et les reliques d'épisodes ont été remises à zéro pour les tests.", ephemeral=True)


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

# CommandTree utilise ce gestionnaire pour les erreurs qui surviennent
# pendant l'exécution des commandes slash.
bot.tree.on_error = _global_app_command_error


# ==========================================
# INITIALISATION UNIQUE DU BOT
# ==========================================

@bot.event
async def on_ready():
    print(f"🤖 Bot connecté en tant que {bot.user} (ID: {bot.user.id})")
    
    # Chargement des succès depuis GitHub
    await load_achievements_from_github()
    
    # Chargement des épisodes depuis GitHub
    await load_episodes_from_github()

    # ------------------------------------------
    # BASE DE DONNÉES
    # ------------------------------------------
    try:
        init_db()
        print("💾 Base économique /data/economy.db : OK")
    except Exception as e:
        print(f"❌ ERREUR INIT BASE DE DONNÉES : {type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------
    # VUES PERSISTANTES
    # ------------------------------------------
    if not getattr(bot, "_persistent_views_registered", False):
        try:
            bot.add_view(PersistentMerchantView())
            bot.add_view(PersistentTroubadourView())
            bot._persistent_views_registered = True
            print("🦊 Vue persistante du Marchand : OK")
            print("🪕 Vue persistante de Guillaume : OK")
        except Exception as e:
            print(f"❌ ERREUR VUES PERSISTANTES : {type(e).__name__}: {e}")
            traceback.print_exc()

    # ------------------------------------------
    # SYNC DES COMMANDES SLASH - ATTENTE POUR ÉVITER LES RATE LIMITS
    # ------------------------------------------
    try:
        # Petit délai pour éviter les rate limits
        await asyncio.sleep(2)

        # Synchronisation globale
        synced_global = await bot.tree.sync()
        print(f"🌲 {len(synced_global)} commandes slash synchronisées globalement.")

        # Synchronisation par serveur
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

    # ------------------------------------------
    # LISTE DES COMMANDES DISPONIBLES
    # ------------------------------------------
    try:
        commands_list = [cmd.name for cmd in bot.tree.get_commands()]
        print(f"📋 Commandes disponibles : {', '.join(commands_list)}")
    except Exception as e:
        print(f"❌ ERREUR LISTE COMMANDES : {e}")

    print("✅ Initialisation terminée : le bot est prêt à recevoir les commandes.")


# ==========================================
# 10. LANCEMENT DU BOT
# ==========================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Token Discord introuvable. Ajoute une variable d'environnement "
            "DISCORD_TOKEN (ou DISCORD_BOT_TOKEN/BOT_TOKEN/TOKEN) dans ton hébergeur. "
            "Ne mets pas le token directement dans le code."
        )
    bot.run(TOKEN)
