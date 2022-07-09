import pathlib
import asyncio
import time
from threading import Thread
import os
import json
import orjson
import os
import time
from binascii import hexlify
from struct import pack
from PIL import Image
import zlib
import base64
import io
from fastapi import FastAPI
from fastapi.responses import Response, ORJSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from gen3_switchgame import Gen3Switchgame
from nintendo.baas import BAASClient
from nintendo.dauth import DAuthClient
from nintendo.aauth import AAuthClient
from nintendo.switch import ProdInfo, KeySet
from nintendo.nex import backend, authentication, settings, datastore_smm2 as datastore
from nintendo.games import SMM2
from anynet import http
from enum import IntEnum

import logging
logging.basicConfig(level=logging.INFO)

args = {}
with open("webserver_args.json") as f:
	args = json.load(f)

if args["system_version"] is None:
	print("System version not set")
	print("Error")
	exit(1)
else:
	SYSTEM_VERSION = args["system_version"]

if args["user_id"] is None:
	print("User ID not set")
	print("Error")
	exit(1)
else:
	BAAS_USER_ID = int(args["user_id"], 16)

if args["password"] is None:
	print("Password not set")
	print("Error")
	exit(1)
else:
	BAAS_PASSWORD = args["password"]

if args["keys"] is None:
	print("Prod.keys not set")
	print("Error")
	exit(1)
else:
	keys = KeySet.load(args["keys"])

if args["prodinfo"] is None:
	print("Prodinfo not set")
	print("Error")
	exit(1)
else:
	info = ProdInfo(keys, args["prodinfo"])

if args["ticket"] is None:
	print("Ticket not set")
	print("Error")
	exit(1)
else:
	with open(args["ticket"], "rb") as f:
		ticket = f.read()

# Used for scraping
debug_enabled = False
if os.environ.get("SERVER_DEBUG_ENABLED") != None:
	print("Server debug enabled")
	debug_enabled = True

class CourseRequestType(IntEnum):
	course_id = 1
	courses_endless_mode = 2
	courses_latest = 3
	courses_point_ranking = 4
	data_ids = 5
	data_ids_no_stop = 6
	search = 7
	posted = 8
	liked = 9
	played = 10
	first_cleared = 11
	world_record = 12

class ServerDataTypes(IntEnum):
	level_thumbnail = 2
	entire_level_thumbnail = 3
	custom_comment_image = 10
	ninji_ghost_replay = 40
	world_map_thumbnails = 50

class ServerDataTypeHeader:
	headers = None
	last_updated = 0
	expiration = 0
	data_type = 0
	def __init__(self, type):
		self.data_type = type
	async def refresh(self, store):
		headers_info = await store.get_req_get_info_headers_info(self.data_type)
		self.headers = {h.key: h.value for h in headers_info.headers}
		self.expiration = headers_info.expiration * 1000
		self.last_updated = milliseconds_since_epoch()
	async def refresh_if_needed(self, store):
		if (milliseconds_since_epoch() - self.last_updated) > (self.expiration - 1000):
			await self.refresh(store)
	async def request_url(self, url, store):
		if (milliseconds_since_epoch() - self.last_updated) > (self.expiration - 1000):
			if store == None:
				return False
			else:
				await self.refresh(store)
		response = await http.get(url, headers=self.headers)
		response.raise_if_error()
		return response.body

class ServerHeaders:
	level_thumbnail = ServerDataTypeHeader(ServerDataTypes.level_thumbnail)
	entire_level_thumbnail = ServerDataTypeHeader(ServerDataTypes.entire_level_thumbnail)
	custom_comment_image = ServerDataTypeHeader(ServerDataTypes.custom_comment_image)
	ninji_ghost_replay = ServerDataTypeHeader(ServerDataTypes.ninji_ghost_replay)
	world_map_thumbnails = ServerDataTypeHeader(ServerDataTypes.world_map_thumbnails)

async def download_thumbnail(store, url, filename, data_type, save = True):
	if data_type == ServerDataTypes.level_thumbnail:
		body = await ServerHeaders.level_thumbnail.request_url(url, store)
		if body == False:
			return False
		else:
			image = Image.open(io.BytesIO(body))
			if save:
				image.save(filename, optimize=True, quality=95)
				return True
			else:
				image_bytes = io.BytesIO()
				image.save(image_bytes, optimize=True, quality=95, format="jpeg")
				return image_bytes.getvalue()

	if data_type == ServerDataTypes.entire_level_thumbnail:
		body = await ServerHeaders.entire_level_thumbnail.request_url(url, store)
		if body == False:
			return False
		else:
			image = Image.open(io.BytesIO(body))
			if save:
				image.save(filename, optimize=True, quality=95)
				return True
			else:
				image_bytes = io.BytesIO()
				image.save(image_bytes, optimize=True, quality=95, format="jpeg")
				return image_bytes.getvalue()

def in_cache(course_id):
	level_info_path = pathlib.Path("cache/level_info/%s" % course_id)
	return level_info_path.exists()

def in_user_cache(maker_id):
	user_info_path = pathlib.Path("cache/user_info/%s" % maker_id)
	return user_info_path.exists()

def invalid_level(course_info):
	if "name" in course_info or "courses" in course_info or "comments" in course_info or "players" in course_info or "deaths" in course_info or "super_worlds" in course_info:
		return False
	else:
		return True

def correct_course_id(course_id):
	return course_id.translate({ord('-'): None, ord(' '): None}).upper()

def invalid_course_id_length(course_id):
	if len(course_id) != 9:
		return True
	charset = "0123456789BCDFGHJKLMNPQRSTVWXY"
	for char in course_id:
		if not char in charset:
			return True
	return False

def course_id_to_dataid(id):
	# https://github.com/kinnay/NintendoClients/wiki/Data-Store-Codes#super-mario-maker-2
	course_id = id[::-1]
	charset = "0123456789BCDFGHJKLMNPQRSTVWXY"
	number = 0
	for char in course_id:
		number = number * 30 + charset.index(char)
	left_side = number
	left_side = left_side << 34
	left_side_replace_mask = 0b1111111111110000000000000000000000000000000000
	number = number ^ ((number ^ left_side) & left_side_replace_mask)
	number = number >> 14
	number = number ^ 0b00010110100000001110000001111100
	return number

def is_maker_id(id):
	# https://github.com/kinnay/NintendoClients/wiki/Data-Store-Codes#super-mario-maker-2
	course_id = id[::-1]
	charset = "0123456789BCDFGHJKLMNPQRSTVWXY"
	number = 0
	for char in course_id:
		number = number * 30 + charset.index(char)
	if number & 8192:
		return True
	return False

def get_mii_data(data):
	# Based on https://github.com/HEYimHeroic/mii2studio/blob/master/mii2studio.py
	user_mii = Gen3Switchgame.from_bytes(data)
	mii_values = [
		user_mii.facial_hair_color,
		user_mii.facial_hair_beard,
		user_mii.body_weight,
		user_mii.eye_stretch,
		user_mii.eye_color,
		user_mii.eye_rotation,
		user_mii.eye_size,
		user_mii.eye_type,
		user_mii.eye_horizontal,
		user_mii.eye_vertical,
		user_mii.eyebrow_stretch,
		user_mii.eyebrow_color,
		user_mii.eyebrow_rotation,
		user_mii.eyebrow_size,
		user_mii.eyebrow_type,
		user_mii.eyebrow_horizontal,
		user_mii.eyebrow_vertical,
		user_mii.face_color,
		user_mii.face_makeup,
		user_mii.face_type,
		user_mii.face_wrinkles,
		user_mii.favorite_color,
		user_mii.gender,
		user_mii.glasses_color,
		user_mii.glasses_size,
		user_mii.glasses_type,
		user_mii.glasses_vertical,
		user_mii.hair_color,
		user_mii.hair_flip,
		user_mii.hair_type,
		user_mii.body_height,
		user_mii.mole_size,
		user_mii.mole_enable,
		user_mii.mole_horizontal,
		user_mii.mole_vertical,
		user_mii.mouth_stretch,
		user_mii.mouth_color,
		user_mii.mouth_size,
		user_mii.mouth_type,
		user_mii.mouth_vertical,
		user_mii.facial_hair_size,
		user_mii.facial_hair_mustache,
		user_mii.facial_hair_vertical,
		user_mii.nose_size,
		user_mii.nose_type,
		user_mii.nose_vertical
	]

	mii_data = b"00"
	mii_bytes = ""
	n = 256
	for v in mii_values:
		n = (7 + (v ^ n)) % 256
		mii_data += hexlify(pack(">B", n))
		mii_bytes += hexlify(pack(">B", v)).decode("ascii")

	url = "https://studio.mii.nintendo.com/miis/image.png?data=" + mii_data.decode("utf-8")
	return [url + "&type=face&width=512&instanceCount=1", mii_bytes]

async def obtain_course_info(course_id, store, noCaching = True):
	param = datastore.GetUserOrCourseParam()
	param.code = course_id
	param.course_option = datastore.CourseOption.ALL

	# Download a specific course
	course_info_json = await get_course_info_json(CourseRequestType.course_id, param, store, noCaching)

	return course_info_json

async def obtain_user_info(maker_id, store, noCaching = True, save = True):
	param = datastore.GetUserOrCourseParam()
	param.code = maker_id
	param.user_option = datastore.UserOption.ALL

	loc = "cache/user_info/%s" % maker_id

	# Prepare directories
	os.makedirs(os.path.dirname(loc), exist_ok=True)

	user_info_path = pathlib.Path(loc)
	if user_info_path.exists() and not noCaching:
		with open(loc, mode="rb") as f:
			return orjson.loads(zlib.decompress(f.read()))
	else:
		if not is_maker_id(maker_id):
			with open(loc, mode="wb+") as f:
				f.write(zlib.compress(('{"error": "Code corresponds to a level", "maker_id": "%s"}' % maker_id).encode("UTF8")))
				return {"error": "Code corresponds to a level", "maker_id": maker_id}
		else:
			try:
				response = await store.get_user_or_course(param)
			except:
				# Save (the empty) level info to json
				print("maker_id " + maker_id + " is invalid")
				with open(loc, mode="wb+") as f:
					f.write(zlib.compress(('{"error": "No user with that ID", "maker_id": "%s"}' % maker_id).encode("UTF8")))
					return {"error": "No user with that ID", "maker_id": maker_id}

		ret = {}
		add_user_info_json(response.user, ret)
		
		if save:
			with open(loc, mode="wb+") as f:
				f.write(zlib.compress(orjson.dumps(ret)))
				return ret

async def search_latest_courses(size, store):
	param = datastore.SearchCoursesLatestParam()
	param.range.offset = 0
	param.range.size = size
	param.option = datastore.CourseOption.ALL

	courses_info_json = await get_course_info_json(CourseRequestType.courses_latest, param, store)

	return courses_info_json

async def get_courses_data_id(data_ids, store):
	param = datastore.GetCoursesParam()
	param.data_ids = data_ids
	param.option = datastore.CourseOption.ALL

	courses_info_json = await get_course_info_json(CourseRequestType.data_ids_no_stop, param, store)

	return courses_info_json

async def get_courses_posted(size, pid, store):
	param = datastore.SearchCoursesPostedByParam()
	param.range.offset = 0
	param.range.size = size
	param.pids = [pid]
	param.option = datastore.CourseOption.ALL

	courses_info_json = await get_course_info_json(CourseRequestType.posted, param, store)

	return courses_info_json

def add_user_info_json(user, json_dict):
	json_dict["region"] = user.region
	json_dict["code"] = user.code
	json_dict["pid"] = str(user.pid)
	json_dict["name"] = user.name
	json_dict["country"] = user.country
	json_dict["last_active"] = user.last_active.timestamp()

	if len(user.unk2) != 0:
		mii_info = get_mii_data(user.unk2)
		if debug_enabled:
			json_dict["mii_data"] = user.unk2
		else:
			json_dict["mii_data"] = base64.b64encode(user.unk2).decode("ascii")
		json_dict["mii_image"] = mii_info[0]
		json_dict["mii_studio_code"] = mii_info[1]

	json_dict["pose"] = user.unk1.unk1
	json_dict["hat"] = user.unk1.unk2
	json_dict["shirt"] = user.unk1.unk3
	json_dict["pants"] = user.unk1.unk4

	if len(user.play_stats) == 4:
		json_dict["courses_played"] = user.play_stats[0]
		json_dict["courses_cleared"] = user.play_stats[2]
		json_dict["courses_attempted"] = user.play_stats[1]
		json_dict["courses_deaths"] = user.play_stats[3]

	if len(user.maker_stats) == 2:
		json_dict["likes"] = user.maker_stats[0]
		json_dict["maker_points"] = user.maker_stats[1]

	if len(user.endless_challenge_high_scores) == 4:
		json_dict["easy_highscore"] = user.endless_challenge_high_scores[0]
		json_dict["normal_highscore"] = user.endless_challenge_high_scores[1]
		json_dict["expert_highscore"] = user.endless_challenge_high_scores[2]
		json_dict["super_expert_highscore"] = user.endless_challenge_high_scores[3]

	if len(user.multiplayer_stats) == 15:
		json_dict["versus_rating"] = user.multiplayer_stats[0]
		json_dict["versus_rank"] = user.multiplayer_stats[1]
		json_dict["versus_won"] = user.multiplayer_stats[3]
		json_dict["versus_lost"] = user.multiplayer_stats[4]
		json_dict["versus_win_streak"] = user.multiplayer_stats[5]
		json_dict["versus_lose_streak"] = user.multiplayer_stats[6]
		json_dict["versus_plays"] = user.multiplayer_stats[2]
		json_dict["versus_disconnected"] = user.multiplayer_stats[7]
		json_dict["coop_clears"] = user.multiplayer_stats[11]
		json_dict["coop_plays"] = user.multiplayer_stats[10]
		json_dict["recent_performance"] = user.multiplayer_stats[12]
		json_dict["versus_kills"] = user.multiplayer_stats[8]
		json_dict["versus_killed_by_others"] = user.multiplayer_stats[9]
		json_dict["multiplayer_stats_unk13"] = user.multiplayer_stats[13]
		json_dict["multiplayer_stats_unk14"] = user.multiplayer_stats[14]

	if len(user.unk8) == 2:
		json_dict["first_clears"] = user.unk8[0]
		json_dict["world_records"] = user.unk8[1]

	if len(user.unk15) == 1:
		json_dict["unique_super_world_clears"] = user.unk15[0]

	if len(user.unk9) == 2:
		json_dict["uploaded_levels"] = user.unk9[0]
		json_dict["maximum_uploaded_levels"] = user.unk9[1]

	if len(user.unk7) == 1:
		json_dict["weekly_maker_points"] = user.unk7[0]

	json_dict["last_uploaded_level"] = user.unk11.timestamp()
	json_dict["is_nintendo_employee"] = user.unk10
	json_dict["comments_enabled"] = user.unk4
	json_dict["tags_enabled"] = user.unk5
	json_dict["super_world_id"] = user.unk14
	json_dict["badges"] = []

	for badge in user.badges:
		badge_info = {}
		badge_info["type"] = badge.unk1
		badge_info["rank"] = badge.unk2
		json_dict["badges"].append(badge_info)

	json_dict["unk3"] = user.unk3
	json_dict["unk12"] = user.unk12
	json_dict["unk16"] = user.unk16

async def add_comment_info_json(store, course_id, course_info, noCaching = True, save = True):
	loc = "cache/level_comments/%s" % course_id
	comments_arr = []

	if pathlib.Path(loc).exists() and not noCaching:
		with open(loc, mode="rb") as f:
			return orjson.loads(zlib.decompress(f.read()))

	user_pids = []
	data_id = course_id_to_dataid(course_id)
	if course_info["num_comments"] < 100:
		comments = await store.search_comments(data_id)
	else:
		# Only 1000 comments are ever available, Nintendo appears to delete them automatically
		param = datastore.SearchCommentsInOrderParam()
		param.range.offset = 0
		param.range.size = 1000
		param.data_id = data_id
		comments = (await store.search_comments_in_order(param)).comments

	for comment in comments:
		if comment.unk5 != 0:
			comment_json = {}
			# Corresponds to course data id, redundant
			#comment_json["unk1"] = comment.unk1
			comment_json["comment_id"] = comment.unk2
			if comment.unk4 == 1:
				comment_json["text"] = comment.unk15
			comment_json["posted_pretty"] = str(comment.unk13)
			comment_json["posted"] = comment.unk13.timestamp()
			comment_json["clear_required"] = comment.unk11
			if comment.unk4 == 2:
				comment_json["reaction_image_id"] = comment.unk16
			comment_json["type"] = comment.unk4
			comment_json["has_beaten"] = bool(comment.unk3)
			comment_json["x"] = comment.unk6
			comment_json["y"] = comment.unk7
			comment_json["reaction_face"] = comment.unk9
			comment_json["unk8"] = comment.unk8 # Usually 0
			comment_json["unk10"] = comment.unk10 # Usually 0
			comment_json["unk12"] = comment.unk12 # Usually false
			if not debug_enabled:
				comment_json["unk14"] = base64.b64encode(comment.unk14).decode("ascii") # Usually nothing
			else:
				comment_json["unk14"] = comment.unk14
			comment_json["unk17"] = comment.unk17 # Usually 0

			if comment.unk4 == 0:
				comment_image = {}
				comment_image["url"] = comment.picture.url
				comment_image["size"] = comment.picture.unk1
				comment_image["filename"] = comment.picture.filename
				comment_json["custom_comment_image"] = comment_image

				# How to extract image
				# response = await http.get(comment.picture.url, headers=custom_comment_image_headers)
				# img = Image.frombuffer("RGBA", (320, 180), zlib.decompress(response.body), "raw", "RGBA", 0, 1)
				# img.save(comment.picture.filename + ".png")

			comments_arr.append(comment_json)
			user_pids.append(comment.unk5)

	if len(user_pids) != 0:
		i = 0
		for users_partial in [user_pids[j:j+500] for j in range(len(user_pids))[::500]]:
			for user_pid in users_partial:
				comments_arr[i]["commenter_pid"] = str(user_pid)
				i += 1

	comments = {}
	comments["comments"] = comments_arr

	if save:
		os.makedirs("cache/level_comments", exist_ok=True)
		with open(loc, mode="wb+") as f:
			f.write(zlib.compress(orjson.dumps(comments)))
	return comments

async def search_world_map(store, ids, noCaching = True, save = True):
	world_map_arr = []

	if len(ids) == 1 and pathlib.Path("cache/super_worlds/%s" % ids[0]).exists() and not noCaching:
		with open("cache/super_worlds/%s" % ids[0], mode="rb") as f:
			return orjson.loads(zlib.decompress(f.read()))

	param = datastore.GetWorldMapParam()
	param.ids = ids
	param.option = 63
	response = await store.get_world_map(param)

	i = 0
	for map in response.maps:
		if map.owner_id == 0:
			continue

		map_json = {}
		map_json["id"] = map.id
		map_json["worlds"] = map.worlds
		map_json["levels"] = map.levels
		map_json["planet_type"] = map.unk2
		map_json["created"] = map.unk3.timestamp()

		map_json["ninjis"] = []
		for element in map.unk4:
			map_json["ninjis"].append(map.unk4[element])

		if debug_enabled and not save:
			map_json["unk1"] = map.unk1
		map_json["unk5"] = map.unk5
		map_json["unk6"] = map.unk6
		map_json["unk7"] = map.unk7

		thumbnail = {}
		thumbnail["url"] = map.thumbnail.url
		thumbnail["size"] = map.thumbnail.size
		thumbnail["filename"] = map.thumbnail.filename
		map_json["thumbnail"] = thumbnail
		map_json["courses"] = map.data_ids

		world_map_arr.append(map_json)
		i += 1

	if save:
		os.makedirs("cache/super_worlds", exist_ok=True)
		for map in world_map_arr:
			with open("cache/super_worlds/%s" % map["id"], mode="wb+") as f:
				f.write(zlib.compress(orjson.dumps(map)))

	world_map_json = {}
	world_map_json["super_worlds"] = world_map_arr
	return world_map_json

async def get_course_info_json(request_type, request_param, store, noCaching = True, save = True):
	courses = []
	from_cache = []
	stop_on_bad = True

	if request_type == CourseRequestType.course_id:
		loc = "cache/level_info/%s" % request_param.code

		# Prepare directories
		os.makedirs(os.path.dirname(loc), exist_ok=True)

		level_info_path = pathlib.Path(loc)
		if level_info_path.exists() and not noCaching:
			with open(loc, mode="rb") as f:
				courses.append(orjson.loads(zlib.decompress(f.read())))
				from_cache.append(True)
		else:
			if invalid_course_id_length(request_param.code):
				# Save (the empty) level info to json
				print("course_id " + request_param.code + " is wrong length")
				with open(loc, mode="wb+") as f:
					f.write(zlib.compress(('{"error": "Invalid course ID", "course_id": "%s"}' % request_param.code).encode("UTF8")))
					return {"error": "Invalid course ID", "course_id": request_param.code}

			if is_maker_id(request_param.code):
				print("course_id " + request_param.code + " is actually maker_id")
				with open(loc, mode="wb+") as f:
					f.write(zlib.compress(('{"error": "Code corresponds to a maker", "course_id": "%s"}' % request_param.code).encode("UTF8")))
					return {"error": "Code corresponds to a maker", "course_id": request_param.code}

			try:
				response = await store.get_user_or_course(request_param)
				if response.user.pid != 0:
					print("course_id " + request_param.code + " is invalid")
					with open(loc, mode="wb+") as f:
						f.write(zlib.compress(('{"error": "No course with that ID", "course_id": "%s"}' % request_param.code).encode("UTF8")))
						return {"error": "No course with that ID", "course_id": request_param.code}
			except:
				# Save (the empty) level info to json
				print("course_id " + request_param.code + " is invalid")
				with open(loc, mode="wb+") as f:
					f.write(zlib.compress(('{"error": "No course with that ID", "course_id": "%s"}' % request_param.code).encode("UTF8")))
					return {"error": "No course with that ID", "course_id": request_param.code}

			courses.append(response.course)
			from_cache.append(False)

	if request_type == CourseRequestType.courses_endless_mode:
		courses = await store.search_courses_endless_mode(request_param)
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.courses_latest:
		response = await store.search_courses_latest(request_param)
		courses = response.courses
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.courses_point_ranking:
		response = await store.search_courses_point_ranking(request_param)
		courses = response.courses
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.data_ids:
		response = await store.get_courses(request_param)
		courses = response.courses
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.data_ids_no_stop:
		response = await store.get_courses(request_param)
		courses = response.courses
		stop_on_bad = False
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.posted:
		response = await store.search_courses_posted_by(request_param)
		courses = response.courses
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.liked:
		courses = await store.search_courses_positive_rated_by(request_param)
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.played:
		courses = await store.search_courses_played_by(request_param)
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.first_cleared:
		response = await store.search_courses_first_clear(request_param)
		courses = response.courses
		from_cache = [None] * len(courses)

	if request_type == CourseRequestType.world_record:
		response = await store.search_courses_best_time(request_param)
		courses = response.courses
		from_cache = [None] * len(courses)

	uploader_pids = []
	first_clear_pids = []
	record_holder_pids = []

	course_info_json = {}
	course_info_json["courses"] = [None] * len(courses)

	i = 0;
	cache_hits = 0
	for course in courses:
		course_info = {}

		if from_cache[i]:
			course_info = course
			cache_hits += 1

			uploader_pids.append(0)
			first_clear_pids.append(0)
			record_holder_pids.append(0)
		else:
			if course.owner_id == 0:
				if stop_on_bad:
					# Invalid data id
					if request_type == CourseRequestType.data_ids:
						return {"error": "No course with that data id", "data_id": request_param.data_ids[i]}
				else:
					continue

			course_info["name"] = course.name
			course_info["description"] = course.description
			course_info["uploaded"] = course.upload_time.timestamp()
			course_info["data_id"] = course.data_id
			course_info["course_id"] = course.code
			course_info["game_style"] = course.game_style
			course_info["theme"] = course.course_theme
			course_info["difficulty"] = course.difficulty
			course_info["tag1"] = course.tag1
			course_info["tag2"] = course.tag2
			if course.time_stats.world_record != 4294967295:
				course_info["world_record"] = course.time_stats.world_record
			course_info["upload_time"] = course.time_stats.upload_time
			course_info["num_comments"] = course.comment_stats[0]
			course_info["clear_condition"] = course.clear_condition
			course_info["clear_condition_magnitude"] = course.clear_condition_magnitude
			if len(course.play_stats) == 5:
				course_info["clears"] = course.play_stats[3]
				course_info["attempts"] = course.play_stats[1]
				if course.play_stats[1] == 0:
					course_info["clear_rate"] = 0
				else:
					course_info["clear_rate"] = (course.play_stats[3] / course.play_stats[1]) * 100
				course_info["plays"] = course.play_stats[0]
				course_info["versus_matches"] = course.play_stats[4]
				course_info["coop_matches"] = course.play_stats[2]
			if len(course.ratings) == 3:
				course_info["likes"] = course.ratings[0]
				course_info["boos"] = course.ratings[1]
				course_info["unique_players_and_versus"] = course.ratings[2]
			if len(course.unk4) == 2:
				course_info["weekly_likes"] = course.unk4[0]
				course_info["weekly_plays"] = course.unk4[1]

			one_screen_thumbnail = {}
			one_screen_thumbnail["url"] = course.one_screen_thumbnail.url
			one_screen_thumbnail["size"] = course.one_screen_thumbnail.size
			one_screen_thumbnail["filename"] = course.one_screen_thumbnail.filename
			course_info["one_screen_thumbnail"] = one_screen_thumbnail

			entire_thumbnail = {}
			entire_thumbnail["url"] = course.entire_thumbnail.url
			entire_thumbnail["size"] = course.entire_thumbnail.size
			entire_thumbnail["filename"] = course.entire_thumbnail.filename
			course_info["entire_thumbnail"] = entire_thumbnail

			course_info["unk2"] = course.unk2
			if debug_enabled and not save:
				course_info["unk3"] = course.unk3
			else:
				course_info["unk3"] = base64.b64encode(course.unk3).decode("ascii")
			course_info["unk9"] = course.unk9
			course_info["unk10"] = course.unk10
			course_info["unk11"] = course.unk11
			course_info["unk12"] = course.unk12

			if pathlib.Path("cache/level_info/%s" % course.code).exists():
				cache_hits += 1

			uploader_pids.append(course.owner_id)
			first_clear_pids.append(course.time_stats.first_completion)
			record_holder_pids.append(course.time_stats.world_record_holder)

		course_info_json["courses"][i] = course_info
		i += 1

	del course_info_json["courses"][i:]

	if store:
		all_pids = uploader_pids + first_clear_pids + record_holder_pids
		all_pids_split = [all_pids[i:i+500] for i in range(len(all_pids))[::500]]
		all_pids_result = []
		if not debug_enabled:
			for pids_chunk in all_pids_split:
				param = datastore.GetUsersParam()
				param.pids = pids_chunk
				param.option = datastore.UserOption.ALL

				all_pids_result += (await store.get_users(param)).users
		if len(uploader_pids) != 0:
			i = 0
			for user_pid in uploader_pids:
				if user_pid != 0:
					course_info_json["courses"][i]["uploader_pid"] = str(user_pid)
				i += 1

		if len(first_clear_pids) != 0:
			i = 0
			for user_pid in first_clear_pids:
				if user_pid != 0:
					course_info_json["courses"][i]["first_completer_pid"] = str(user_pid)
				i += 1

		if len(record_holder_pids) != 0:
			i = 0
			for user_pid in record_holder_pids:
				if user_pid != 0:
					course_info_json["courses"][i]["record_holder_pid"] = str(user_pid)
				i += 1

		if save:
			i = 0
			os.makedirs("cache/level_info", exist_ok=True)
			for course in course_info_json["courses"]:
				if not from_cache[i]:
					loc = "cache/level_info/%s" % course["course_id"]
					with open(loc, mode="wb+") as f:
						f.write(zlib.compress(orjson.dumps(course)))

				i += 1

	if request_type == CourseRequestType.course_id:
		return course_info_json["courses"][0]
	else:
		course_info_json["cache_hits"] = cache_hits
		return course_info_json


HOST = "g%08x-lp1.s.n.srv.nintendo.net" % SMM2.GAME_SERVER_ID
PORT = 443
s = None
user_id = None
auth_info = None
device_token_generated_time = None
id_token_generated_time = None
device_token = None
app_token = None
access_token = None
id_token = None
getting_credentials = asyncio.Lock()

def milliseconds_since_epoch():
	return time.time_ns() // 1000000

async def check_tokens():
	global HOST
	global PORT
	global s
	global user_id
	global auth_info
	global device_token_generated_time
	global id_token_generated_time
	global device_token
	global app_token
	global access_token
	global id_token
	global getting_credentials
	if getting_credentials.locked():
		# Another thread is busy refreshing the credentials, wait until it is done and return
		async with getting_credentials:
			return
	# Either has never been generated or is older than 23.9 hours
	if device_token_generated_time is None or (milliseconds_since_epoch() - device_token_generated_time) > 85340000:
		async with getting_credentials:
			cert = info.get_tls_cert()
			pkey = info.get_tls_key()

			print("Generate device token")
			dauth = DAuthClient(keys)
			dauth.set_certificate(cert, pkey)
			dauth.set_system_version(SYSTEM_VERSION)
			response = await dauth.device_token(dauth.BAAS)
			device_token = response["device_auth_token"]
			print("Generated device token")

			print("Generate app token")
			aauth = AAuthClient()
			aauth.set_system_version(SYSTEM_VERSION)
			response = await aauth.auth_digital(
				SMM2.TITLE_ID, SMM2.LATEST_VERSION,
				device_token, ticket
			)
			app_token = response["application_auth_token"]
			print("Generated app token")

			device_token_generated_time = milliseconds_since_epoch()

			id_token = None
			print("Generate id token")
			baas = BAASClient()
			baas.set_system_version(SYSTEM_VERSION)
			response = await baas.authenticate(device_token)
			access_token = response["accessToken"]
			response = await baas.login(BAAS_USER_ID, BAAS_PASSWORD, access_token, app_token)
			id_token = response["idToken"]
			user_id = str(int(response["user"]["id"], 16))
			print("Generated id token")

			id_token_generated_time = milliseconds_since_epoch()

			auth_info = authentication.AuthenticationInfo()
			auth_info.token = id_token
			auth_info.ngs_version = 4
			auth_info.token_type = 2

			print("Loading settings")
			s = settings.load("switch")
			s.configure(SMM2.ACCESS_KEY, SMM2.NEX_VERSION, SMM2.CLIENT_VERSION)
			print("Loaded settings")
	# Either has never been generated or is older than 2.9 hours
	if id_token_generated_time is None or (milliseconds_since_epoch() - id_token_generated_time) > 1044000:
		async with getting_credentials:
			print("Generate id token")
			baas = BAASClient()
			baas.set_system_version(SYSTEM_VERSION)
			response = await baas.authenticate(device_token)
			access_token = response["accessToken"]
			response = await baas.login(BAAS_USER_ID, BAAS_PASSWORD, access_token, app_token)
			id_token = response["idToken"]
			user_id = str(int(response["user"]["id"], 16))
			print("Generated id token")

			id_token_generated_time = milliseconds_since_epoch()

			auth_info = authentication.AuthenticationInfo()
			auth_info.token = id_token
			auth_info.ngs_version = 4
			auth_info.token_type = 2

			print("Loading settings")
			s = settings.load("switch")
			s.configure(SMM2.ACCESS_KEY, SMM2.NEX_VERSION, SMM2.CLIENT_VERSION)
			print("Loaded settings")


async def main():
	print("Running API setup")
	await check_tokens()

class AsyncLoopThread(Thread):
	def __init__(self):
		super().__init__(daemon=True)
		self.loop = asyncio.new_event_loop()

	def run(self):
		asyncio.set_event_loop(self.loop)
		self.loop.run_forever()

app = FastAPI(openapi_url=None)
lock = asyncio.Semaphore(3)

app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

if "banned_ips" in args:
	banned_ips = args["banned_ips"]
else:
	banned_ips = []
@app.middleware("http")
async def add_process_time_header(request, call_next):
	if str(request.client.host) in banned_ips:
		return ORJSONResponse(status_code=400, content={})
	return await call_next(request)

print("Start FastAPI")

@app.get("/level_info/{course_id}")
async def read_level_info(course_id: str, nocaching: bool = True):
	course_id = correct_course_id(course_id)
	if (in_cache(course_id) or invalid_course_id_length(course_id) or is_maker_id(course_id)) and not noCaching:
		course_info_json = await obtain_course_info(course_id, None)

		if invalid_level(course_info_json):
			return ORJSONResponse(status_code=400, content=course_info_json)

		return ORJSONResponse(content=course_info_json)
	else:
		await check_tokens()
		async with lock:
			async with backend.connect(s, HOST, PORT) as be:
				async with be.login(str(user_id), auth_info=auth_info) as client:
					store = datastore.DataStoreClientSMM2(client)
					print("Want course info for " + course_id)
					course_info_json = await obtain_course_info(course_id, store, noCaching)

					if invalid_level(course_info_json):
						return ORJSONResponse(status_code=400, content=course_info_json)

					return ORJSONResponse(content=course_info_json)

@app.get("/user_info/{maker_id}")
async def read_user_info(maker_id: str, nocaching: bool = True):
	maker_id = correct_course_id(maker_id)
	if (in_user_cache(maker_id) or invalid_course_id_length(maker_id) or not is_maker_id(maker_id)) and not noCaching:
		user_info_json = await obtain_user_info(maker_id, None)

		if invalid_level(user_info_json):
			return ORJSONResponse(status_code=400, content=user_info_json)

		return ORJSONResponse(content=user_info_json)
	else:
		await check_tokens()
		async with lock:
			async with backend.connect(s, HOST, PORT) as be:
				async with be.login(str(user_id), auth_info=auth_info) as client:
					store = datastore.DataStoreClientSMM2(client)
					print("Want user info for " + maker_id)
					user_info_json = await obtain_user_info(maker_id, store, noCaching)

					if invalid_level(user_info_json):
						return ORJSONResponse(status_code=400, content=user_info_json)

					return ORJSONResponse(content=user_info_json)

@app.get("/level_info_multiple/{data_ids}")
async def read_level_infos(data_ids: str):
	corrected_data_ids = []
	for id in data_ids.split(","):
		corrected_data_ids.append(int(id))

	if len(corrected_data_ids) > 500:
		return ORJSONResponse(status_code=400, content={"error": "Number of courses requested must be between 1 and 500"})

	await check_tokens()
	async with lock:
		async with backend.connect(s, HOST, PORT) as be:
			async with be.login(str(user_id), auth_info=auth_info) as client:
				store = datastore.DataStoreClientSMM2(client)
				print("Want course infos for " + data_ids)
				course_info_json = await get_courses_data_id(corrected_data_ids, store)

				if invalid_level(course_info_json):
					return ORJSONResponse(status_code=400, content=course_info_json)

				return ORJSONResponse(content=course_info_json)

@app.get("/user_info_multiple/{pids}")
async def user_info_multiple(pids: str):
	corrected_pids = []
	for id in pids.split(","):
		corrected_pids.append(int(id))

	if len(corrected_pids) > 500:
		return ORJSONResponse(status_code=400, content={"error": "Number of pids requested must be between 1 and 500"})

	await check_tokens()
	async with lock:
		async with backend.connect(s, HOST, PORT) as be:
			async with be.login(str(user_id), auth_info=auth_info) as client:
				store = datastore.DataStoreClientSMM2(client)
				print("Want user infos for " + pids)

				# Get user info for all pids				
				param = datastore.GetUsersParam()
				param.pids = corrected_pids
				param.option = datastore.UserOption.ALL
				response = await store.get_users(param)

				# Filter out invalid users
				# Can be determined by checking for a pid of 0
				valid_users = []
				for user in response.users:
					if user.pid != 0:
						valid_users.append(user)

				# Put user info into a JSON object
				user_info_json = {"users": []}
				i = 0
				for user in valid_users:
					user_info_json["users"].append({})
					add_user_info_json(user, user_info_json["users"][i])
					i += 1

				return ORJSONResponse(content=user_info_json)

@app.get("/level_comments/{course_id}")
async def read_level_comments(course_id: str, nocaching: bool = True):
	course_id = correct_course_id(course_id)
	print("Want comments for " + course_id)
	path = "cache/level_comments/%s" % course_id

	if invalid_course_id_length(course_id):
		return ORJSONResponse(status_code=400, content={"error": "Invalid course ID", "course_id": course_id})
	if is_maker_id(course_id):
		return ORJSONResponse(status_code=400, content={"error": "Code corresponds to a maker", "course_id": course_id})

	os.makedirs(os.path.dirname(path), exist_ok=True)

	if pathlib.Path(path).exists() and not noCaching:
		comments = await add_comment_info_json(None, course_id, None)
		if invalid_level(comments):
			return ORJSONResponse(status_code=400, content=comments)
		return ORJSONResponse(content=comments)
	else:
		await check_tokens()
		async with lock:
			async with backend.connect(s, HOST, PORT) as be:
				async with be.login(str(user_id), auth_info=auth_info) as client:
					store = datastore.DataStoreClientSMM2(client)
					course_info_json = await obtain_course_info(course_id, store)
					if invalid_level(course_info_json):
						return ORJSONResponse(status_code=400, content=course_info_json)
					comments = await add_comment_info_json(store, course_id, course_info_json, noCaching)
					if invalid_level(comments):
						return ORJSONResponse(status_code=400, content=comments)
					return ORJSONResponse(content=comments)

@app.get(
	"/level_thumbnail/{course_id}",
	responses = {
		200: {
			"content": {"image/jpg": {}}
		}
	},
	response_class=Response
)
async def read_level_thumbnail(course_id: str):
	course_id = correct_course_id(course_id)
	# Download thumbnails
	print("Want thumbnail for " + course_id)
	path = "cache/level_thumbnail/%s.jpg" % course_id
	os.makedirs(os.path.dirname(path), exist_ok=True)

	if pathlib.Path(path).exists():
		return FileResponse(path=path, media_type="image/jpg")

	course_info_json = None
	if in_cache(course_id) or invalid_course_id_length(course_id):
		course_info_json = await obtain_course_info(course_id, None)
		if invalid_level(course_info_json):
			return ORJSONResponse(status_code=400, content=course_info_json)

	if in_cache(course_id) and await download_thumbnail(None, course_info_json["one_screen_thumbnail"]["url"], path, ServerDataTypes.level_thumbnail):
		return FileResponse(path=path, media_type="image/jpg")
	else:
		await check_tokens()
		async with lock:
			async with backend.connect(s, HOST, PORT) as be:
				async with be.login(str(user_id), auth_info=auth_info) as client:
					store = datastore.DataStoreClientSMM2(client)
					if course_info_json == None:
						course_info_json = await obtain_course_info(course_id, store)
					if invalid_level(course_info_json):
						return ORJSONResponse(status_code=400, content=course_info_json)
					await download_thumbnail(store, course_info_json["one_screen_thumbnail"]["url"], path, ServerDataTypes.level_thumbnail)
					return FileResponse(path=path, media_type="image/jpg")

@app.get(
	"/level_data/{data_id}",
	responses = {
		200: {
			"content": {"application/octet-stream": {}}
		}
	},
	response_class=Response
)
async def read_level_data(data_id: int):
	print("Want course data for dataid %d" % data_id)
	loc = "cache/level_data_dataid/%s.bcd" % data_id
	os.makedirs(os.path.dirname(loc), exist_ok=True)

	if pathlib.Path(loc).exists():
		if os.stat(loc).st_size == 0:
			os.remove(loc)
			return ORJSONResponse(status_code=400, content={"error": "Level data file cannot be downloaded", "data_id": data_id})
		else:
			return FileResponse(path=loc, media_type="application/octet-stream")

	await check_tokens()
	async with lock:
		async with backend.connect(s, HOST, PORT) as be:
			async with be.login(str(user_id), auth_info=auth_info) as client:
				store = datastore.DataStoreClientSMM2(client)
				param = datastore.DataStorePrepareGetParam()
				param.data_id = data_id
				try:
					req_info = await store.prepare_get_object(param)
				except:
					with open(loc, "wb") as f:
						return ORJSONResponse(status_code=400, content={"error": "Level data file cannot be downloaded", "data_id": data_id})
				response = await http.get(req_info.url)
				response.raise_if_error()
				with open(loc, "wb") as f:
					f.write(response.body)
					return Response(content=response.body, media_type="application/octet-stream")

@app.get("/get_posted/{maker_id}")
async def search_posted(maker_id: str):
	maker_id = correct_course_id(maker_id)

	user_info_json = None
	if (in_user_cache(maker_id) or invalid_course_id_length(maker_id) or not is_maker_id(maker_id)):
		user_info_json = await obtain_user_info(maker_id, None)
		if invalid_level(user_info_json):
			return ORJSONResponse(status_code=400, content=user_info_json)

	await check_tokens()
	async with lock:
		async with backend.connect(s, HOST, PORT) as be:
			async with be.login(str(user_id), auth_info=auth_info) as client:
				store = datastore.DataStoreClientSMM2(client)
				print("Want uploaded courses from %s" % maker_id)
				if user_info_json == None:
					user_info_json = await obtain_user_info(maker_id, store)

				courses_info_json = await get_courses_posted(100, user_info_json["pid"], store)

				if invalid_level(courses_info_json):
					return ORJSONResponse(status_code=400, content=courses_info_json)

				return ORJSONResponse(content=courses_info_json)

@app.get("/super_worlds/{map_ids}")
async def get_world_maps(map_ids: str, nocaching: bool = True):
	corrected_map_ids = []
	for id in map_ids.split(","):
		corrected_map_ids.append(id)

	# TODO: Make this work with multiple world IDs
	
	await check_tokens()
	async with lock:
		async with backend.connect(s, HOST, PORT) as be:
			async with be.login(str(user_id), auth_info=auth_info) as client:
				store = datastore.DataStoreClientSMM2(client)
				print("Want world maps %s" % map_ids)
				world_maps = await search_world_map(store, corrected_map_ids)

				if invalid_level(world_maps):
					return ORJSONResponse(status_code=400, content=world_maps)

				return ORJSONResponse(content=world_maps)

@app.get("/newest_data_id")
async def newest_data_id():
	count = 100
	await check_tokens()
	async with lock:
		async with backend.connect(s, HOST, PORT) as be:
			async with be.login(str(user_id), auth_info=auth_info) as client:
				store = datastore.DataStoreClientSMM2(client)
				print("Want %d latest courses" % count)
				courses_info_json = await search_latest_courses(count, store)

				if invalid_level(courses_info_json):
					return ORJSONResponse(status_code=400, content=courses_info_json)

				# Calculate max data_id from the JSON array
				max_data_id = 0
				for course_info in courses_info_json["courses"]:
					if course_info["data_id"] > max_data_id:
						max_data_id = course_info["data_id"]

				# Put the max data_id into a new JSON object
				return ORJSONResponse(content={"data_id": max_data_id})

loop_handler = AsyncLoopThread()
loop_handler.start()
asyncio.run_coroutine_threadsafe(main(), loop_handler.loop)