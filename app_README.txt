GTA IV Exporter
===============

1. Double-click "GTA IV Exporter.exe".
2. Settings: check the GTA IV folder (found automatically on most machines) and pick where
   to export:
   - Blender: needs Blender 4.2+ installed; choose where to save the .blend.
   - Unreal Engine: needs Unreal Engine 5 installed; pick your .uproject and a level name.
     Close Unreal Editor first. Everything goes into Content/GTAIV in that project.
     Tip: use a separate project, the full city adds about 9 GB of assets.
3. Districts: add the districts you want (or "Select all" for the whole city).
4. Click "Export", follow it on the Export page, then "Open in Blender" / "Open in Unreal".

Keep the .exe next to the "_internal" folder.
Blender: textures are saved in a "<name>_textures" folder beside the .blend; move them together.

Characters (Unreal, "Characters & weapons"): all GTA IV peds and Niko go to
Content/GTAIV/Characters (one shared skeleton), weapons to Content/GTAIV/Meshes (SM_WPN_*).
In a Game Animation Sample project you also get:
  - BP_GTAIV_Ped: a GASP visual override showing a random GTA IV ped
  - GM_GTAIV: GASP's game mode with that override switched on (the level uses it), so the
    player is Niko and NPCs are random peds
  - GTAIV_Peds actor in the level: pedestrians walking the sidewalks (MaxPeds, SpawnRadius...)
