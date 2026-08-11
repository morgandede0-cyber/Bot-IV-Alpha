import os
import discord
from discord.ext import commands

# On importe les configurations, instances et mini-jeux depuis bot.py
from bot import bot, init_db, BankView, JimTavernView, JohnCrimeView, BobArenaView

# On importe les vues persistantes du marchand et du troubadour depuis ShopIV.py
from ShopIV import PersistentMerchantView, PersistentTroubadourView

# Événement de démarrage global unifié
@bot.event
async def on_ready():
    # Initialisation des bases de données des deux scripts
    init_db()
    
    # Enregistrement de toutes les vues persistantes des PNJ et des boutiques
    bot.add_view(PersistentMerchantView())
    bot.add_view(PersistentTroubadourView())
    bot.add_view(BankView())
    bot.add_view(JimTavernView())
    bot.add_view(JohnCrimeView())
    bot.add_view(BobArenaView())
    
    print(f"🤖 Bot IV fusionné et connecté en tant que {bot.user} (ID: {bot.user.id})")
    
    try:
        synced = await bot.tree.sync()
        print(f"🌲 {len(synced)} commandes slash synchronisées avec succès.")
    except Exception as e:
        print(f"❌ Erreur lors de la synchronisation des commandes : {e}")

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    bot.run(TOKEN)
