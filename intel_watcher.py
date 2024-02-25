import argparse
import sys
import requests
import time
import logging
import coloredlogs

from time import sleep
from concurrent.futures.thread import ThreadPoolExecutor

from intelwatcher.ingress import IntelMap, get_tiles, maybe_byte
from intelwatcher.config import Config
from intelwatcher.queries import Queries
from intelwatcher.get_cookie import mechanize_cookie, selenium_cookie
from intelwatcher.stopwatch import Stopwatch


MAX_FAILS = 8

def get_bbox():
    bboxes = []
    areas_no = 0
    loaded = []

    if config.bbox:
        bboxes = list(config.bbox.split(';'))
        bbox_no = len(bboxes)
        bboxes = [tuple(map(float, bbox.split(','))) for bbox in bboxes]
        log.info(f"BBox loaded {bbox_no} areas")

    if config.koji_project:
        bboxes = []  # clean if we care about koji
        session = requests.Session()
        if config.koji_bearer:
            session.headers = {'Authorization': "Bearer " + config.koji_bearer}
        koji_req = session.get(config.koji_project)
        koji_data = koji_req.json()

        include_types = []
        if config.koji_include:
            if "," in config.koji_include:
                include_types = config.koji_include.splitf(",")
            else:
                include_types = [config.koji_include]

        for area in koji_data["data"]["features"]:
            if include_types:
                if area.get("properties") and area["properties"].get("type") in include_types:
                    bbox = area.get("bbox")
                    if bbox:
                        areas_no += 1
                        loaded.append(area.get("properties", {}).get("name", "No name"))
                        bboxes.append(tuple(bbox))
            else:
                bbox = area.get("bbox")
                if bbox:
                    areas_no += 1
                    loaded.append(area.get("properties", {}).get("name", "No name"))
                    bboxes.append(tuple(bbox))

        log.info(f"Koji loaded {areas_no} areas: {', '.join(loaded)}")

    return list(set(bboxes))


def update_wp(wp_type, points):
    updated = 0
    log.info(f"Found {len(points)} {wp_type}s")
    for wp in points:
        portal_details = scraper.get_portal_details(wp[0])
        if portal_details is not None:
            try:
                pname = maybe_byte(portal_details.get("result")[portal_name])
                queries.update_point(wp_type, pname, maybe_byte(portal_details.get("result")[portal_url]), wp[0])
                updated += 1
                log.info(f"Updated {wp_type} {pname}")
            except Exception as e:
                log.error(f"Could not update {wp_type} {wp[0]}")
                log.exception(e)
        else:
            log.info(f"Couldn't get Portal info for {wp_type} {wp[0]}")
            
    log.info(f"Updated {updated} {wp_type}s")
    log.info("")


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def needed_tiles(tiles):
    return [t for t in tiles if not t.success and t.fails < MAX_FAILS]


def scrape_eta(iteration, start_time, all_times, all_tiles_no):
    last_100_times = all_times[-100:]
    current_time = time.time()
    average_iteration_time = sum(last_100_times) / len(last_100_times)
    remaining_iterations = all_tiles_no - iteration
    total_time_elapsed = current_time - start_time
    eta_seconds = remaining_iterations * average_iteration_time
    eta = current_time + eta_seconds
    total_time_elapsed_formatted = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(total_time_elapsed))
    eta_formatted = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(eta))
    return (
        f"Iteration: {iteration}/{all_tiles_no} ETA: {eta_seconds}s "
        f"Elapsed: {total_time_elapsed_formatted} Target: {eta_formatted}"
    )


def tiles_stats(tiles):
    def formatted_fail_metrics(d):
        return ' | '.join([f"{key} = {value: <8}" for key, value in d.items()])

    total_tiles = len(tiles)
    success_tiles = len([t for t in tiles if t.success])
    failed = {}
    recovered = {}
    for fails_number in range(1, MAX_FAILS):
        failed[fails_number] = len([t for t in tiles if not t.success and t.fails == fails_number])
    for fails_number in range(1, MAX_FAILS):
        recovered[fails_number] = len([t for t in tiles if t.success and t.fails == fails_number])
    return (
        f"Total: {total_tiles} Success: {success_tiles}\nFailed: {formatted_fail_metrics(failed)}"
        f"\nRecove: {formatted_fail_metrics(recovered)}"
    )


def scrape_all(n):
    tiles = []
    iteration = 0
    bbox = get_bbox()
    for cord in bbox:
        tiles += get_tiles(cord)

    all_tiles_no = len(tiles)
    tiles = list(set(tiles))
    dedupe_tiles_no = len(tiles)

    start_time = time.time()
    all_times = []
    log.info(f"Total tiles: {dedupe_tiles_no}. Removed {all_tiles_no - dedupe_tiles_no} duplicated tiles.")

    portals = []
    while len(needed_tiles(tiles)) > 0:
        part_tiles = needed_tiles(tiles)[:config.maxtiles]
        scrapetime = Stopwatch()
        iteration_time = time.time()

        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            for part_tile in chunks(part_tiles, n):
                executor.submit(scraper.scrape_tiles, part_tile, portals, log)

        log.info(f"Done in {scrapetime.pause()}s - Writing portals to DB")

        failed_tiles = [t for t in part_tiles if t.failed]
        if len(failed_tiles) > 0:
            log.warning(f"There were {len(failed_tiles)} tiles that failed")
            for tile in failed_tiles:
                tile.tries = 0
                tile.fails += 1

        queries = Queries(config)
        try:
            queries.update_portal(portals)
        except Exception as e:
            log.error(f"Failed executing Portal Inserts")
            log.exception(e)

        log.info(f"Updated {len(portals)} Portals")

        queries.close()

        if len(tiles) > config.maxtiles:
            portals = []
            success_tiles = len([t for t in tiles if t.success])
            log.info(f"Total tiles scraped: {success_tiles}/{len(tiles)}")

            next_tiles = needed_tiles(tiles)
            if len(next_tiles) > 0:
                log.info((f"Sleeping {config.areasleep} minutes before getting the next "
                          f"{len(next_tiles[:config.maxtiles])} tiles"))

                total_sleep = 60 * config.areasleep
                all_times += time.time() - iteration_time
                sleep(total_sleep)
                iteration += 1
                if iteration % 10 == 0:
                    log.info(scrape_eta(iteration, start_time, all_times, all_tiles_no))
                    log.info(tiles_stats(tiles))


def send_cookie_webhook(text):
    if config.cookie_wh:
        data = {
            "username": "Cookie Alarm",
            "avatar_url": ("https://emojipedia-us.s3.dualstack.us-west-1.amazonaws.com/thumbs/120/"
                           "apple/237/cookie_1f36a.png"),
            "content": config.cookie_text,
            "embeds": [{
                "description": f":cookie: {text}",
                "color": 16073282
            }]
        }
        result = requests.post(config.wh_url, json=data)
        log.info(f"Webhook response: {result.status_code}")


if __name__ == "__main__":
    portal_name = 8
    portal_url = 7

    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--update", action='store_true', help="Updates all Gyms and Stops using Portal info")
    parser.add_argument("-c", "--config", default="config.ini", help="Config file to use")
    parser.add_argument("-w", "--workers", default=0, help="Workers")
    parser.add_argument("-d", "--debug", action='store_true', help="Run the script in debug mode")
    parser.add_argument("-t", "--tiles", default=15, help="How many tiles to scrape per worker")
    args = parser.parse_args()

    # LOG STUFF
    success_level = 25
    if args.debug:
        log_level = "DEBUG"
    else:
        log_level = "INFO"

    log = logging.getLogger(__name__)
    logging.addLevelName(success_level, "SUCCESS")
    def success(self, message, *args, **kws):
        self._log(success_level, message, args, **kws) 
    logging.Logger.success = success
    logHandler = logging.StreamHandler(sys.stdout)
    logHandler.setLevel(logging.INFO)
    log.addHandler(logHandler)
    coloredlogs.DEFAULT_LEVEL_STYLES["debug"] = {"color": "blue"}
    coloredlogs.install(level=log_level, logger=log, fmt="%(message)s")

    log.info("Initializing...")

    config_path = args.config

    config = Config(config_path)

    scraper = IntelMap(config.cookie, config)

    if not scraper.getCookieStatus():
        log.error("Oops! Looks like you have a problem with your cookie.")
        cookie_get_success = False
        if config.enable_cookie_getting:
            log.info("Trying to get a new one")
            while not cookie_get_success:
                try:
                    if config.cookie_getting_module == "mechanize":
                        config.cookie = mechanize_cookie(config, log)
                        cookie_get_success = True

                    elif config.cookie_getting_module == "selenium":
                        config.cookie = selenium_cookie(config, log)
                        cookie_get_success = True
                except Exception as e:
                    log.error(("Error while trying to get a Cookie - sending a webhook, "
                               "sleeping 1 hour and trying again"))
                    log.exception(e)
                    send_cookie_webhook(("Got an error while trying to get a new cookie - Please check logs. "
                                         "Retrying in 1 hour."))
                    time.sleep(3600)
            scraper.login(config.cookie)
        else:
            send_cookie_webhook("Your Intel Cookie probably ran out! Please get a new one or check your account.")
            sys.exit(1)
    else:
        log.success("Cookie works!")

    log.success("Got everything. Starting to scrape now.")

    if args.update:
        queries = Queries(config)
        gyms = queries.get_empty_gyms()
        stops = queries.get_empty_stops()
        update_wp("Gym", gyms)
        update_wp("Stop", stops)
        queries.close()
        sys.exit()

    if int(args.workers) > 0:
        config.workers = int(args.workers)

    if int(args.tiles) > 25:
        log.error("Please use a -t count below 25")
        sys.exit(1)

    time = Stopwatch()
    scrape_all(int(args.tiles))
    log.success(f"Total runtime: {time.pause()} seconds")
