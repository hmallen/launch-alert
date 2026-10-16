# Launch Alert for Unraid

This directory packages Launch Alert for Unraid's Docker manager and a future [Community Applications](https://github.com/unraid/community.applications) submission. The app runs in the background and sends ntfy notifications 30 minutes before eligible rocket launches with livestreams. It has no web interface or published ports.

The template is prepared for `ghcr.io/hmallen/launch-alert:latest`. **The image must be published and made public before other people can install it.** A template in this repository does not make the app available in Community Applications.

## Install and test on your Unraid server

1. Enable Docker in Unraid's **Settings → Docker**. Keep the server running and its clock synchronized, with outbound HTTPS access to `ll.thespacedevs.com` and your ntfy server.
2. Download [launch-alert.xml](https://raw.githubusercontent.com/hmallen/launch-alert/main/templates/launch-alert.xml) to `/boot/config/plugins/dockerMan/templates-user/my-launch-alert.xml` on the server. Create the `templates-user` directory if necessary. For an unpublished checkout, copy its `templates/launch-alert.xml` to that path instead.
3. In **Docker → Add Container**, select the `Launch-Alert` user template. Enable **Advanced View** to inspect all settings.
4. Set **Private ntfy topic** to a long, random value from a password manager. Subscribe your devices to that same topic in the ntfy app or at `https://ntfy.sh/YOUR_TOPIC`, then enable notification permissions and background delivery. Keep this topic private: anyone who knows an unprotected topic can subscribe and publish to it.
5. Review the appdata path, notification server, lead time, and display timezone. Leave **Display time zone** blank to use your Unraid server's timezone, or enter an [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) such as `Europe/London` for local times in messages.
6. Apply the template, enable the container's **Autostart** switch, and inspect its logs. After the first successful schedule refresh, Docker health should become healthy.
7. From the Unraid terminal, send a real test notification:

   ```sh
   docker exec Launch-Alert python -m launch_alert test-notification
   docker exec Launch-Alert python -m launch_alert status
   ```

   Verify receipt on every device while locked or idle. The container name in these commands must match the name in your template.

If the image has not been published yet, build a local image from a checkout on the Unraid server:

```sh
docker build -t launch-alert:local .
```

In the template, temporarily set **Repository** to `launch-alert:local` and install it using the steps above. This validates your local build; repeat the installation with the published GHCR image before submission.

## Settings and persistent data

| Template setting | Default | Purpose |
| --- | --- | --- |
| Appdata | `/mnt/user/appdata/launch-alert` | Maps to `/config`; contains the SQLite state and delivery history. |
| ntfy server | `https://ntfy.sh` | Server used by both the service and your devices. |
| Private ntfy topic | Required; blank | Private random topic shared with your devices. |
| ntfy access token | Empty | Optional bearer token for authenticated publishing. |
| Reminder lead time (minutes) | `30` | Minutes before the latest scheduled launch time. |
| Schedule refresh interval (seconds) | `300` | Interval between launch schedule refreshes. |
| Display time zone | Empty | Uses Unraid's timezone; set explicitly to override. Scheduling uses UTC. |
| Maximum data age (seconds) | `900` | Stops reminders when schedule data becomes stale. |

Advanced settings include an optional Launch Library API token. Masked token fields hide their display in the form; Unraid still stores the values in its container configuration. Protect the flash drive, appdata backups, and server access accordingly.

The template's `--user=99:100` runs the process as Unraid's `nobody:users`. Unraid creates new appdata directories with the appropriate ownership; an existing directory must also be writable by that identity. If permissions need repair, stop the container and correct ownership on this app's directory only. There are no `PUID` or `PGID` variables.

When installing through Community Applications, Unraid may replace the declared appdata path with your configured appdata root plus the container name (for example, `/mnt/user/appdata/Launch-Alert`). Check the path shown before applying the template. A local user-template installation can retain the declared lowercase path.

Updates preserve the appdata directory. Stop the container before backing it up; retain its SQLite database to preserve reminder history and pending retries. Use one instance per database. Unraid's normal image update controls work once the image is public. Restart or recreate the container after changing its configuration.

The app's health endpoint stays inside the container; use Docker health and the `status` command above. An unhealthy status does not itself restart a container. Host outages and device push settings can prevent alerts; see [device setup and reliability details](../README.md#reliability-and-scope), including Android background delivery and self-hosted ntfy's iOS requirements.

## Publish and submit to Community Applications

1. Keep the source repository public at [hmallen/launch-alert](https://github.com/hmallen/launch-alert), including its MIT license, icon, [`ca_profile.xml`](../ca_profile.xml), and [template](../templates/launch-alert.xml). Update the repository, maintainer, support, icon, and registry URLs together if publishing under another account.
2. In GitHub **Actions → Publish image → Run workflow**, enter a version such as `v1.0.0`, or push a matching release tag. The [workflow](../.github/workflows/publish-image.yml) builds `linux/amd64` and `linux/arm64` images after verification. Stable versions update `latest`; prereleases such as `v1.1.0-rc.1` publish only their full version tag. After the first publication, set the GHCR package's visibility to **Public** in GitHub's package settings; a public source repository alone does not make its package public.
3. Verify the release can be pulled without registry credentials. On a machine without saved GHCR credentials, run:

   ```sh
   docker pull ghcr.io/hmallen/launch-alert:latest
   ```

   Test each advertised architecture on suitable hardware. Install the published image through the template on a current Unraid release, confirm appdata permissions, restart persistence, health status, and delivery to locked devices, and record the Unraid version tested.
4. Run the repository checks before submission:

   ```sh
   python3 tools/validate_unraid.py
   python3 -m unittest discover -s tests -v
   python3 tools/integration_smoke.py
   docker build -t launch-alert:unraid .
   python3 tools/unraid_smoke.py
   ```

5. Use the [Community Applications submission portal](https://ca.unraid.net/submit). Submit `https://github.com/hmallen/launch-alert` as the repository, follow the portal's **Validate** and **Scan** checks, address reported issues, then submit for review. Approval and availability in Apps are controlled by Community Applications. Follow the current [official starter instructions](https://github.com/unraid/unraid-community-apps-starter) if requirements change.

Suggested description for the submission:

> Launch Alert sends ntfy notifications to phones and computers shortly before rocket launches that have a livestream. It follows Launch Library 2 schedule changes, persists delivery state in appdata, and retries temporary notification failures. The default lead time is 30 minutes. Requires a configured ntfy topic and an always-on server; no web interface or inbound ports are needed.

Use [GitHub Issues](https://github.com/hmallen/launch-alert/issues) for app support. Publishing the image, completing a real Unraid installation, and submitting the repository are release steps; they are not performed by the local validation scripts.

The template is marked beta until it has passed a real Unraid installation and locked-device delivery test. Remove the `<Beta>true</Beta>` flag when that release testing is complete. Local container smoke checks exercise permissions, appdata persistence, and health with fixtures; they do not contact the launch API or send real notifications.
