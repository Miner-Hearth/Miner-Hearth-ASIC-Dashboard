# Miner Hearth v1.17.1 live update: how to apply and test it

You need: Miner Hearth **v1.17.0** running on your PC, and the file **MinerHearth-update-v1.17.1.zip** from this folder.
It takes about 5 minutes. Your miners keep running the whole time.

What v1.17.1 adds:
- a firelit stone wall behind the dashboard,
- a switch in Settings to turn that background off,
- a small label at the bottom right of the dashboard showing the version you are running ("Miner Hearth v1.17.1").

## 1. Apply the update
1. Open Miner Hearth. Click **Settings** (top right).
2. Scroll down to the box called **Live Updates**.
3. Tick **Allow live updates**, then click the **Save** button just below it.
4. Next to **Update package**, click the file button (**Choose File**) and pick `MinerHearth-update-v1.17.1.zip`.
5. Optional, to be sure you have the right file: paste this into **Expected SHA-256 (optional)**:
   `46eadcb233cbae526804b47e26aa140bd55a3b625340ad72ab0262fdc0d3090e`
6. Click **Check package**. It should show version **1.17.1**. "Unsigned" is expected for this test package.
7. Click **Apply update**. The page reloads by itself after a second, and the stone wall background appears straight away.
   The header still says v1.17.0 at this point. That's normal: the rest needs a restart.

## 2. Quick restart
1. In the same **Live Updates** box, click **Quick restart** (next to Apply update).
2. Wait about 10 seconds. The window closes and reopens by itself.
3. Check: the version next to "Miner Hearth" at the top says **v1.17.1**, and at the very bottom right of the dashboard it says **Miner Hearth v1.17.1**.

If the window hasn't come back after about 30 seconds, open Miner Hearth again from the Start menu. Nothing is lost.

## 3. Test the background switch
1. Click **Settings**, then find the **Appearance** box (Theme and Temperatures are in it).
2. Untick **Dashboard background**. The stone wall goes away at once and you get the plain background.
3. Tick it again and the wall comes back. Your choice is saved, so it stays the same after a restart.
   (With Theme set to **High contrast**, the background is never shown.)

## 4. Roll back to v1.17.0
1. Settings, then **Live Updates**, then click **Roll back**. Click **OK** when it asks "Go back to the version before the last update?".
2. It says "Rolled back: now 1.17.0 (as shipped). Use Quick restart to finish."
3. Click **Quick restart** and wait for the window to reopen.
4. Check: the top says **v1.17.0**, the label at the bottom right is **gone**, and so are the stone wall and the Dashboard background switch.
   That confirms the rollback worked.

To try again, apply the zip again (steps 1.4 to 1.7). **Remove all updates** clears every live update and leaves plain v1.17.0.
