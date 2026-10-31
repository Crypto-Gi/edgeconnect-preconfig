# Step 11 Ubuntu-over-SSH check (preconfig GUI)

This replaces the WSL check, since there is no Windows machine. The GUI runs on the Ubuntu host, and your Mac's browser reaches it through an SSH tunnel. That is the "GUI on another Linux box" case, which CI does not cover.

Open two Mac terminals.
- **Terminal 1** holds the SSH session to Ubuntu: blocks 1 to 4 run there.
- **Terminal 2** is only for the tunnel.

The blocks clone the `gui` branch (1.4.0-rc.1 when this check was written) into a scratch folder on Ubuntu, and do not touch any toolkit install. Uploads stay off, so nothing is uploaded to any Orchestrator. The build in check 4 reads one appliance with GET only, and asks your validator's `/validate`, which changes nothing.

If block 1 says `ensurepip is not available`, run `sudo apt install -y python3-venv` and then run block 1 again.

## Block 1: install the gui branch (Terminal 1, on Ubuntu)

```bash
git clone --branch gui https://github.com/Crypto-Gi/edgeconnect-preconfig.git ~/ec-preconfig-gui-test
cd ~/ec-preconfig-gui-test
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -q -r requirements.txt
git log -1 --format='%h %s'
cat toolkit/VERSION
python -c "import sys; print(sys.version.split()[0])"
```

Expected:
- the commit line;
- the version of the `gui` branch you cloned, such as `1.4.0-rc.2`;
- a Python version of 3.9 or later.

## Block 2: give it your Orchestrators

Copy your own `.env` to the Ubuntu host, for example from the Mac with `scp /path/to/.env <user>@<ubuntu>:ec-preconfig-gui-test/.env`.
- Don't paste keys here.
- Your API keys will sit on that host until you remove the folder at the end.
- Use an `.env` whose Orchestrators the Ubuntu host can reach. If it can't reach one, that one simply shows red.
- Do not add any `_gui_preconfig_uploads=on` line, so the GUI cannot upload.

Then run:

```bash
cd ~/ec-preconfig-gui-test
chmod 600 .env
grep -ci "_gui_preconfig_uploads *= *\(on\|true\|yes\|1\)" .env
grep -c "^EC_VALIDATORS=" .env
```

Expected: `0`, then `1`. There must be one validator line, so the build can be checked.

## Block 3: start it and open it through the tunnel

**Terminal 1 (Ubuntu):**

```bash
cd ~/ec-preconfig-gui-test
. .venv/bin/activate
python3 -m toolkit.gui --no-browser --port 8765
```

It first prints four folders: `.env`, output, work and run log. The `.env` line should be `/home/<user>/ec-preconfig-gui-test/.env`, written out in full. Then it prints the `http://127.0.0.1:8765/?token=...` link. Leave it running.

**Terminal 2 (Mac), the tunnel.** Use the same port number on both sides:

```bash
ssh -N -L 8765:127.0.0.1:8765 <user>@<ubuntu>
```

Copy the link from Terminal 1 into your Mac browser.

Check 0 (expected refusal): first try a tunnel with a different local port. Stop the tunnel with Ctrl+C and run `ssh -N -L 9000:127.0.0.1:8765 <user>@<ubuntu>`. Open `http://localhost:9000/`. It must be refused with "this server only answers to 127.0.0.1, localhost or [::1] on its own port". This is the DNS-rebinding guard. Stop that tunnel and start the 8765 one again.

Then check these in the browser:

1. The page opened from the printed link through the tunnel. The top bar reads "EdgeConnect Preconfig". Opening the same link a second time is refused, because it works once.
2. The **Orchestrators** panel shows each one as green, amber or red with a reason, and each says `uploads off`. One that is off-VPN being red is fine. Your validator says `validator yes`.
3. Pick a reachable Orchestrator in the top switcher, open **Inventory**, and wait. It lists the appliances and the preconfigs already there. Click one preconfig: its YAML opens with secrets shown as `<SECRET:...>`. (If there are none, write "no preconfigs".)
4. Go back to all tasks, open **Build a template from an appliance**, press **Find appliances**, pick a lab appliance, keep your validator under **Validate on**, and press **Build**. It takes about a minute, and the progress counts the requests. It ends with "Saved <appliance>, validated on <validator>" and the summary.
5. Press **Choose what changes per site** and wait for "... columns per site". Tick one more box. The count goes up by one or more, and the right side highlights that value in the YAML. Press **Save template and CSV**, type `sshcheck`, and press **Save**. The Mac browser downloads `sshcheck.jinja2` and `sites.csv` (to the Mac's Downloads folder, over the tunnel).
6. Go back to all tasks and open **Make the preconfig files**. Choose `sshcheck.jinja2` and `sites.csv` from your Downloads folder. Keep **Create the YAML files** and press **Make the files**. It ends with "Done", and the note says every cell was checked. The table has one row, written, with a `.yml` link. Click it, and the file downloads.
7. On the same screen, choose **Create, validate and upload** and press **Make the files** again. It is refused at once: `Not started: GUI uploads are off for <nickname>: set <nickname>_gui_preconfig_uploads=on in .env to allow them`. Nothing is sent.
8. Press **Theme** in the top bar. The page switches between light and dark, and everything stays readable.
9. Press Ctrl+C in Terminal 1. It prints `GUI stopped.` Then stop the tunnel in Terminal 2.

## Block 4: what it wrote

```bash
cd ~/ec-preconfig-gui-test
ls preconfig-output/
ls preconfig-output/runs/*/rendered/
ls -l preconfig-output/runs/*/
wc -l reports/preconfig.jsonl
```

Expected: the appliance folder and `runs`, one `.yml` under `rendered/`, the run folder's `template.jinja2` and `sites.csv` shown as `-rw-------`, and a run log of a few lines (the list, build, save and make runs; the previews of check 5 log nothing).

## What to send back

Send one line per check: the commit, VERSION and Python version from block 1, check 0, checks 1 to 9, each pass or fail, and the block 4 listing with names replaced. For any fail, include what the terminal or page said, with names, addresses and serials replaced.

To remove it afterwards, run `rm -rf ~/ec-preconfig-gui-test`.
