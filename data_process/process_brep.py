import os 
import pickle 
import argparse
from tqdm import tqdm
from multiprocessing.pool import Pool
from convert_utils import *
from occwl.io import load_step

# import boto3
import subprocess

# To speed up processing, define maximum threshold
MAX_FACE = 30
max_ctrl_setting=10
max_uvlength_setting=10


def normalize(surf_ctrlPts, edge_pnts, corner_pnts):
    """
    Various levels of normalization 
    """
    # Global normalization to -1~1
    total_points = np.array(surf_ctrlPts).reshape(-1, 4)
    non_pad_idx = np.where(total_points[:,3]>0)
    min_vals = np.min(total_points[list(non_pad_idx[0]), :3], axis=0)
    max_vals = np.max(total_points[list(non_pad_idx[0]), :3], axis=0)
    global_offset = min_vals + (max_vals - min_vals)/2 
    global_scale = max(max_vals - min_vals)
    assert global_scale != 0, 'scale is zero'

    surfs_wcs, edges_wcs, surfs_ncs, edges_ncs = [],[],[],[]

    # Normalize corner 
    corner_wcs = (corner_pnts - global_offset[np.newaxis,:]) / (global_scale * 0.5)
  
    # Normalize surface Nurbs
    for surf_ctrlPt in surf_ctrlPts:    
        # Normalize CAD to WCS
        non_pad_idx = np.where(surf_ctrlPt[:,:,3]>0)
        non_pad_u = len(list(set(non_pad_idx[0])))
        non_pad_v = len(list(set(non_pad_idx[1])))
        non_pad_pw = surf_ctrlPt[:non_pad_u, :, :]
        non_pad_pw = non_pad_pw[:, :non_pad_v, :]
        surf_weight = non_pad_pw[:, :, 3:] * 2 - 1
        surf_ctrlPt_wcs = (non_pad_pw[:, :, :3] - global_offset[np.newaxis, np.newaxis, :]) / (global_scale * 0.5)
        surf_pw_wcs = np.concatenate([surf_ctrlPt_wcs, surf_weight], -1)    
        pw_wcs = np.ones((max_ctrl_setting, max_ctrl_setting, 4)) * (-1)
        pw_wcs[:surf_pw_wcs.shape[0], :surf_pw_wcs.shape[1],:] = surf_pw_wcs
        surfs_wcs.append(pw_wcs)
      
        # Normalize Surface to NCS
        min_vals = np.min(surf_ctrlPt_wcs.reshape(-1,3), axis=0)
        max_vals = np.max(surf_ctrlPt_wcs.reshape(-1,3), axis=0)
        local_offset = (max_vals + min_vals)/2 
        local_scale = max(max_vals - min_vals)
        pnt_ncs = (surf_ctrlPt_wcs - local_offset[np.newaxis,np.newaxis,:]) / (local_scale * 0.5)
        surf_pw_ncs = np.concatenate([pnt_ncs, surf_weight], -1) 
        pw_ncs = np.ones((max_ctrl_setting, max_ctrl_setting, 4)) * (-1)
        pw_ncs[:surf_pw_ncs.shape[0], :surf_pw_ncs.shape[1],:] = surf_pw_ncs
        surfs_ncs.append(pw_ncs)
      
    # Normalize edge
    for edge_pnt in edge_pnts:    
        # Normalize CAD to WCS
        edge_pnt_wcs = (edge_pnt - global_offset[np.newaxis,:]) / (global_scale * 0.5)
        edges_wcs.append(edge_pnt_wcs)
        # Normalize Edge to NCS
        min_vals = np.min(edge_pnt_wcs.reshape(-1,3), axis=0)
        max_vals = np.max(edge_pnt_wcs.reshape(-1,3), axis=0)
        local_offset = min_vals + (max_vals - min_vals)/2 
        local_scale = max(max_vals - min_vals)
        pnt_ncs = (edge_pnt_wcs - local_offset) / (local_scale * 0.5)
        edges_ncs.append(pnt_ncs)
        assert local_scale != 0, 'scale is zero'

    surfs_wcs = np.stack(surfs_wcs)
    surfs_ncs = np.stack(surfs_ncs)
    edges_wcs = np.stack(edges_wcs)
    edges_ncs = np.stack(edges_ncs)

    return surfs_wcs, edges_wcs, surfs_ncs, edges_ncs, corner_wcs


def parse_solid(solid):
    """
    Parse the surface, curve, face, edge, vertex in a CAD solid.
   
    Args:
    - solid (occwl.solid): A single brep solid in occwl data format.

    Returns:
    - data: A dictionary containing all parsed data
    """
    assert isinstance(solid, Solid)

    # Split closed surface and closed curve to halve
    solid = solid.split_all_closed_faces(num_splits=0)
    solid = solid.split_all_closed_edges(num_splits=0)

    if len(list(solid.faces())) > MAX_FACE:
        return None
        
    # Extract all B-rep primitives and their adjacency information
    extracted_data = extract_primitive(solid, max_ctrl_setting, max_uvlength_setting)
    if extracted_data is None:
      return None
    else:
      face_ctrlPts, face_ukvs, face_vkvs, face_pnts, edge_pnts, edge_corner_pnts, edgeFace_IncM, faceEdge_IncM = extracted_data
    
    # Normalize the CAD model
    surfs_wcs, edges_wcs, surfs_ncs, edges_ncs, corner_wcs = normalize(face_ctrlPts, edge_pnts, edge_corner_pnts)

    # Remove duplicate and merge corners 
    corner_wcs = np.round(corner_wcs, 4) 
    corner_unique = []
    for corner_pnt in corner_wcs.reshape(-1,3):
        if len(corner_unique) == 0:
            corner_unique = corner_pnt.reshape(1,3)
        else:
            # Check if it exist or not 
            exists = np.any(np.all(corner_unique == corner_pnt, axis=1))
            if exists:
                continue 
            else:
                corner_unique = np.concatenate([corner_unique, corner_pnt.reshape(1,3)], 0)

    # Edge-corner adjacency  
    edgeCorner_IncM = []
    for edge_corner in corner_wcs:
        start_corner_idx = np.where((corner_unique == edge_corner[0]).all(axis=1))[0].item()
        end_corner_idx = np.where((corner_unique == edge_corner[1]).all(axis=1))[0].item()
        edgeCorner_IncM.append([start_corner_idx, end_corner_idx])
    edgeCorner_IncM = np.array(edgeCorner_IncM)
  
    # Surface UV KnotVectors
    surf_ukvs = face_ukvs*2-1
    surf_vkvs = face_vkvs*2-1
  
    
    # Surface global bbox
    surf_bboxes = []
    for pnts in surfs_wcs:
        flat_pnts = pnts.reshape(-1,4)
        non_pad_idx = np.where(flat_pnts[:,3]>-1)
        flat_pnts = flat_pnts[list(non_pad_idx[0]), :3]
        min_point, max_point = get_bbox(flat_pnts)
        surf_bboxes.append(np.concatenate([min_point, max_point]))
    surf_bboxes = np.vstack(surf_bboxes)

    # Edge global bbox
    edge_bboxes = []
    for pnts in edges_wcs:
        min_point, max_point = get_bbox(pnts.reshape(-1,3))
        edge_bboxes.append(np.concatenate([min_point, max_point]))
    edge_bboxes = np.vstack(edge_bboxes)

    # Convert to float32 to save space
    data = {
        'surf_wcs':surfs_wcs.astype(np.float32), #1
        'edge_wcs':edges_wcs.astype(np.float32), #2
        'surf_ncs':surfs_ncs.astype(np.float32), #3
        'surf_ukvs':surf_ukvs.astype(np.float32), #4
        'surf_vkvs':surf_vkvs.astype(np.float32), #5
        'edge_ncs':edges_ncs.astype(np.float32), #6
        'corner_wcs':corner_wcs.astype(np.float32), #7
        'edgeFace_adj': edgeFace_IncM, #8
        'edgeCorner_adj':edgeCorner_IncM, #9
        'faceEdge_adj':faceEdge_IncM, #10
        'surf_bbox_wcs':surf_bboxes.astype(np.float32), #11
        'edge_bbox_wcs':edge_bboxes.astype(np.float32), #12
        'corner_unique':corner_unique.astype(np.float32), #13
    }

    return data


def process(step_folder):
    """
    Helper function to load step files and process in parallel

    Args:
    - step_folder (str): Path to the STEP parent folder.

    Returns:
    - Complete status: Valid (1) / Non-valid (0).
    """
    try:
      # Load cad data
      # print(step_folder)
      if step_folder.endswith('.step'):
        step_path = step_folder 
        process_furniture = True
        
      elif 'abc' in step_folder:
        for _, _, files in os.walk(step_folder):
            assert len(files) == 1 
            step_path = os.path.join(step_folder, files[0])
        process_furniture = False

      else:
        step_path = step_folder + '.step'
        process_furniture = False
      
      # Check single solid
      cad_solid = load_step(step_path)
      
      if len(cad_solid)!=1: 
        #   print('Skipping multi solids...')
          return 0 
          
      # Start data parsing
      data = parse_solid(cad_solid[0])
      if data is None: 
        #   print ('Exceeding threshold...')
          return 0 # number of faces or edges exceed pre-determined threshold
  
      # Save the parsed result 
      if process_furniture:
          data_uid = step_path.split('/')[-2] + '_' + step_path.split('/')[-1]
          sub_folder = step_path.split('/')[-3]
      else:
          data_dir = step_path.split('/')[-2]
          sub_folder = data_dir[:4]
          data_uid = step_path.split('/')[-1]
          
      if data_uid.endswith('.step'):
          data_uid = data_uid[:-5] # furniture avoid .step
  
      data['uid'] = data_uid
      save_folder = os.path.join(OUTPUT, sub_folder)
      if not os.path.exists(save_folder):
          os.makedirs(save_folder)
  
      save_path = os.path.join(save_folder, data['uid']+'.pkl')
      with open(save_path, "wb") as tf:
          pickle.dump(data, tf)
  
      return 1 

    except Exception as e:
        print(e)  
        # print('not saving due to error...')
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, help="Data folder path", required=True)
    parser.add_argument("--option", type=str, choices=['abc', 'deepcad', 'furniture'], default='abc', 
                        help="Choose between dataset option [abc/deepcad/furniture] (default: abc)")
    parser.add_argument("--interval", type=int, help="Data range index, only required for abc/deepcad")
    args = parser.parse_args()    

    if args.option == 'deepcad': 
        OUTPUT = 'deepcad_parsed'
    elif args.option == 'abc': 
        OUTPUT = 'abc_parsed'
    else:
        OUTPUT = 'furniture_parsed'
      
    # Load all STEP files
    if args.option == 'furniture':
        step_dirs = load_furniture_step(args.input)
    if args.option == 'deepcad':
        step_dirs = load_deepcad_step(args.input)
        step_dirs = step_dirs[args.interval*10000 : (args.interval+1)*10000]
    else:
        step_dirs = load_abc_step(args.input, args.option == 'deepcad')
        step_dirs = step_dirs[args.interval*10000 : (args.interval+1)*10000]
    
    print(step_dirs)
    # Process B-reps in parallel
    valid = 0
    convert_iter = Pool(os.cpu_count()).imap(process, step_dirs) 
    for status in tqdm(convert_iter, total=len(step_dirs)):
        valid += status 
    print(f'Done... Data Converted Ratio {100.0*valid/len(step_dirs)}%')


