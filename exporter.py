import requests
import sys
import json
import os
import concurrent.futures
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

def get_sporttech_results(event_id, target_club="Robertson Gymnastics Club", data_dir=None):
    base_url = f"https://sporttech.io/events/{event_id}/ovs/api"
    
    print(f"Fetching data for Event ID: {event_id}...")
    
    # Configure a robust requests session with retries
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))

    # 1. Fetch Event Metadata
    try:
        event_resp = session.get(f"{base_url}/event")
        event_resp.raise_for_status()
        event_json = event_resp.json()
        event_data = event_json.get('Event', {})
    except Exception as e:
        print(f"Error fetching event metadata: {e}")
        return None

    event_title = event_data.get('Title', 'N/A')
    event_date = event_data.get('StartDate', 'N/A')
    print(f"\nEvent: {event_title}")
    print(f"Date: {event_date}")
    print("-" * 60)

    # 2. Fetch all Competitions (Bulk API call)
    try:
        comps_resp = session.get(f"{base_url}/competitions")
        comps_resp.raise_for_status()
        comps_data = comps_resp.json().get('Competitions', {})
    except Exception as e:
        print(f"Error fetching competitions: {e}")
        return None

    # Extract all Stage IDs and map StageID -> Competition Title and ID
    stage_ids = set()
    stage_to_comp = {}
    for cid, comp in comps_data.items():
        for stage_id in comp.get('Stages', []):
            stage_ids.add(stage_id)
            stage_to_comp[stage_id] = {
                'id': cid,
                'title': comp.get('Title')
            }

    print(f"Found {len(stage_ids)} stages across {len(comps_data)} competitions.")
    print("Fetching stage results in parallel...")

    results_by_athlete = {}

    # 3. Fetch each Stage in parallel with bulk query parameters
    def fetch_stage_data(sid):
        url = (
            f"{base_url}/stages/{sid}?"
            "fetch_performance_frames=true&"
            "fetch_stage_groups=true&"
            "fetch_group_performances=true&"
            "fetch_performance_athletes=true&"
            "fetch_competition_stages=true"
        )
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                return sid, r.json()
        except Exception as e:
            print(f"Warning: Failed to fetch stage {sid}: {e}")
        return sid, None

    # Fetch stages concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch_stage_data, sid) for sid in stage_ids]
        for future in concurrent.futures.as_completed(futures):
            sid, data = future.result()
            if not data:
                continue
            
            comp_info = stage_to_comp.get(sid, {'id': 'N/A', 'title': 'Unknown'})
            
            athletes = data.get('Athletes', {})
            perfs = data.get('Performances', {})
            frames = data.get('Frames', {})
            
            for aid, ath in athletes.items():
                if aid not in results_by_athlete:
                    results_by_athlete[aid] = {
                        'id': aid,
                        'name': f"{ath.get('GivenName', '')} {ath.get('Surname', '')}".strip(),
                        'club': ath.get('Representing', 'Unknown'),
                        'results': []
                    }
                
                # Match performance for this athlete in this stage
                for pid, perf in perfs.items():
                    perf_athletes = perf.get('Athletes', [])
                    # Check if athlete is in this performance
                    is_participant = False
                    for pa in perf_athletes:
                        if str(pa) == str(aid):
                            is_participant = True
                            break
                    
                    if is_participant:
                        total_score = perf.get('MarkTTT_G', 0) / 1000.0
                        rank = perf.get('Rank_G', 'N/A')
                        
                        # Gather frame/routine scores
                        frame_scores = []
                        for fid in perf.get('Frames', []):
                            f_obj = frames.get(str(fid))
                            if f_obj and f_obj.get('State') == 3:  # Published state
                                frame_scores.append(f_obj.get('MarkTTT_G', 0) / 1000.0)
                        
                        # To avoid duplicate results for the same athlete in the same competition and stage,
                        # check if we already registered this exact stage-competition pair.
                        # Some APIs may repeat data across queries.
                        already_exists = False
                        for existing_res in results_by_athlete[aid]['results']:
                            if existing_res['competition_id'] == comp_info['id'] and existing_res.get('stage_id') == sid:
                                already_exists = True
                                break
                        
                        if not already_exists:
                            results_by_athlete[aid]['results'].append({
                                'competition_title': comp_info['title'],
                                'competition_id': comp_info['id'],
                                'stage_id': sid,
                                'total_score': total_score,
                                'rank': rank,
                                'routine_scores': frame_scores
                            })

    # 4. Restructure data for JSON export
    competitions_dict = {}
    athletes_dict = {}

    def get_rank_sort_key(res):
        rank = res.get('rank')
        try:
            return (0, int(rank))
        except (ValueError, TypeError):
            return (1, rank)

    for aid, info in results_by_athlete.items():
        athletes_dict[aid] = {
            'id': aid,
            'name': info['name'],
            'club': info['club'],
            'results': []
        }
        
        for r in info['results']:
            # Append to athlete view results
            athletes_dict[aid]['results'].append({
                'competition_id': r['competition_id'],
                'competition_title': r['competition_title'],
                'total_score': r['total_score'],
                'rank': r['rank'],
                'routine_scores': r['routine_scores']
            })
            
            # Append to competition view results
            cid = r['competition_id']
            if cid not in competitions_dict:
                competitions_dict[cid] = {
                    'id': cid,
                    'title': r['competition_title'],
                    'results': []
                }
            
            competitions_dict[cid]['results'].append({
                'athlete_id': aid,
                'athlete_name': info['name'],
                'club': info['club'],
                'total_score': r['total_score'],
                'rank': r['rank'],
                'routine_scores': r['routine_scores']
            })

    # Sort results in competitions by rank
    for cid in competitions_dict:
        competitions_dict[cid]['results'].sort(key=get_rank_sort_key)

    # Sort results for athletes by competition title
    for aid in athletes_dict:
        athletes_dict[aid]['results'].sort(key=lambda x: x['competition_title'])

    # Prepare files paths
    if data_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(script_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)

    # Save detailed event data
    event_data_file = os.path.join(data_dir, f"{event_id}.json")
    detailed_payload = {
        'event_id': event_id,
        'title': event_title,
        'date': event_date,
        'competitions': competitions_dict,
        'athletes': athletes_dict
    }
    
    with open(event_data_file, 'w', encoding='utf-8') as f:
        json.dump(detailed_payload, f, indent=2, ensure_ascii=False)
    print(f"Detailed data saved to {event_data_file}")

    # Update events.json index
    events_file = os.path.join(data_dir, 'events.json')
    events_list = []
    if os.path.exists(events_file):
        try:
            with open(events_file, 'r', encoding='utf-8') as f:
                events_list = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load existing events.json: {e}")
            events_list = []

    # Check if event already exists in index and update, else append
    updated = False
    for ev in events_list:
        if ev.get('event_id') == event_id:
            ev['title'] = event_title
            ev['date'] = event_date
            updated = True
            break
    if not updated:
        events_list.append({
            'event_id': event_id,
            'title': event_title,
            'date': event_date
        })

    with open(events_file, 'w', encoding='utf-8') as f:
        json.dump(events_list, f, indent=2, ensure_ascii=False)
    print(f"Events index updated in {events_file}")

    # 5. Print target club summary to console (for backward compatibility/quick check)
    target_athletes = [aid for aid, info in results_by_athlete.items() if info['club'] == target_club]
    print(f"\nFound results for {len(target_athletes)} athletes representing {target_club}:\n")
    print("=" * 60)

    for aid in sorted(target_athletes, key=lambda x: results_by_athlete[x]['name']):
        info = results_by_athlete[aid]
        print(f"Athlete: {info['name']} (ID: {aid})")
        print("-" * 60)
        for r in sorted(info['results'], key=lambda x: x['competition_title']):
            routine_str = ", ".join(f"{s:.3f}" for s in r['routine_scores']) if r['routine_scores'] else "N/A"
            print(f"  • {r['competition_title']} (Comp ID: {r['competition_id']})")
            print(f"    Total Score: {r['total_score']:.3f} | Rank: {r['rank']}")
            print(f"    Routine Scores: {routine_str}")
            print()
        print("=" * 60)

    return detailed_payload

if __name__ == "__main__":
    default_event_id = "abe4589c-87cd-41cc-5176-46a06287aa6b"
    default_club = "Robertson Gymnastics Club"
    
    event_id = sys.argv[1] if len(sys.argv) > 1 else default_event_id
    club = sys.argv[2] if len(sys.argv) > 2 else default_club
    
    get_sporttech_results(event_id, club)
