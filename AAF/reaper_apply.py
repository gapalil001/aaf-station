# @noindex
"""REAPER API bridge; kept separate so rollback and placement can be tested."""
from ctypes import CFUNCTYPE, c_byte, c_uint64
import math
from pathlib import Path


def valid_pointer(value):
    if value is None or value == 0 or value == "":
        return False
    if isinstance(value, str) and "0x" in value:
        return int(value.rsplit("0x", 1)[1], 16) != 0
    return bool(value)


def source_transfer(api):
    # The generated Python wrappers reject PCM_source pointers until REAPER
    # owns them. Call these two entry points directly for our freshly created
    # source, whose lifetime is controlled by this import.
    get_function = getattr(api, "rpr_getfp", None)
    if get_function is None:
        return api.RPR_SetMediaItemTake_Source, api.RPR_PCM_Source_Destroy
    attach = CFUNCTYPE(c_byte, c_uint64, c_uint64)(get_function("SetMediaItemTake_Source"))
    destroy = CFUNCTYPE(None, c_uint64)(get_function("PCM_Source_Destroy"))

    def address(pointer):
        return int(pointer.rsplit("0x", 1)[1], 16)

    def attach_source(take, source):
        return attach(address(take), address(source))

    def destroy_source(source):
        destroy(address(source))

    return attach_source, destroy_source


def apply_plan(api, plan, project):
    """Append tracks in one undo block; delete new tracks if any API call fails."""
    def call(name, *args):
        return getattr(api, "RPR_" + name)(*args)

    if plan.get("version") != 1 or not plan.get("tracks"):
        raise ValueError("Invalid/empty import plan")
    current = call("EnumProjects", -1, "", 4096)[0]
    if current != project:
        raise ValueError("Active REAPER project changed. Select the original tab and run the importer again.")
    for track in plan["tracks"]:
        for clip in track["clips"]:
            if not Path(clip["path"]).is_file():
                raise ValueError("Missing prepared audio: " + clip["path"])
            for key in ("position", "duration", "offset", "gain", "fade_in", "fade_out"):
                if not math.isfinite(clip[key]) or clip[key] < 0:
                    raise ValueError("Invalid " + key + " in " + clip["name"])
    attach_source, destroy_source = source_transfer(api)
    created = []
    succeeded = False
    call("Undo_BeginBlock2", project)
    call("PreventUIRefresh", 1)
    try:
        for row in plan["tracks"]:
            index = call("CountTracks", project)
            call("InsertTrackAtIndex", index, False)
            track = call("GetTrack", project, index)
            if not valid_pointer(track):
                raise RuntimeError("Could not create track")
            created.append(track)
            call("GetSetMediaTrackInfo_String", track, "P_NAME", row["name"], True)
            call("SetMediaTrackInfo_Value", track, "I_NCHAN", 2)
            call("SetMediaTrackInfo_Value", track, "D_VOL", 1.0)
            call("SetMediaTrackInfo_Value", track, "D_PAN", 0.0)
            call("SetMediaTrackInfo_Value", track, "D_PANLAW", 1.0)
            call("SetMediaTrackInfo_Value", track, "D_WIDTH", 1.0)
            call("SetMediaTrackInfo_Value", track, "I_PANMODE", 0)
            for clip in row["clips"]:
                source = call("PCM_Source_CreateFromFile", clip["path"])
                if not valid_pointer(source):
                    raise RuntimeError("REAPER cannot open: " + clip["path"])
                attached = False
                try:
                    item = call("AddMediaItemToTrack", track)
                    if not valid_pointer(item):
                        raise RuntimeError("REAPER could not create an audio item")
                    take = call("AddTakeToMediaItem", item)
                    if not valid_pointer(take):
                        raise RuntimeError("REAPER could not create a take")
                    if not attach_source(take, source):
                        raise RuntimeError("REAPER could not attach source audio")
                    attached = True
                    take_source = call("GetMediaItemTake_Source", take)
                    length, _, is_qn = call("GetMediaSourceLength", take_source, False)
                    sample_rate = call("GetMediaSourceSampleRate", take_source)
                    if is_qn:
                        raise ValueError("Expected audio source in seconds")
                    if length <= 0 or sample_rate <= 0:
                        raise ValueError("REAPER could not read the attached audio: " + clip["path"])
                    end = clip["offset"] + clip["duration"]
                    if end > length + 1.01 / sample_rate:
                        raise ValueError("Clip %r ends at %.9f s, but its source is %.9f s: %s" %
                                         (clip["name"], end, length, clip["path"]))
                    if (row.get("stereo") or clip.get("stereo")) and call("GetMediaSourceNumChannels", take_source) != 2:
                        raise ValueError("Prepared stereo file does not contain two channels")
                    for key, value in (("D_POSITION", clip["position"]), ("D_LENGTH", clip["duration"]),
                                       ("B_LOOPSRC", 0), ("C_BEATATTACHMODE", 0),
                                       ("D_FADEINLEN", clip["fade_in"]), ("D_FADEOUTLEN", clip["fade_out"]),
                                       ("D_FADEINLEN_AUTO", -1), ("D_FADEOUTLEN_AUTO", -1),
                                       ("C_FADEINSHAPE", 0), ("C_FADEOUTSHAPE", 0)):
                        if not call("SetMediaItemInfo_Value", item, key, value):
                            raise RuntimeError("REAPER rejected item setting: " + key)
                    for key, value in (("D_STARTOFFS", clip["offset"]), ("D_VOL", clip["gain"]),
                                       ("D_PAN", 0), ("D_PANLAW", 1), ("D_PLAYRATE", 1), ("I_CHANMODE", 0)):
                        if not call("SetMediaItemTakeInfo_Value", take, key, value):
                            raise RuntimeError("REAPER rejected take setting: " + key)
                    call("GetSetMediaItemTakeInfo_String", take, "P_NAME", clip["name"], True)
                    call("UpdateItemInProject", item)
                finally:
                    if not attached:
                        destroy_source(source)
        succeeded = True
    except Exception:
        for track in reversed(created):
            call("DeleteTrack", track)
        raise
    finally:
        call("PreventUIRefresh", -1)
        call("TrackList_AdjustWindows", False)
        call("UpdateArrange")
        label = "Import AAF session data" if succeeded else "AAF import cancelled (new tracks removed)"
        call("Undo_EndBlock2", project, label, -1)
    return len(created), sum(len(t["clips"]) for t in plan["tracks"])
