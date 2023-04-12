import os
import numpy as np
import pandas as pd
import json
import datetime
import xml.etree.ElementTree as ET
import shutil

import freewayControl as fc
import connectedEnv as cv
import queue_estimator as qe

import traci
from sumolib import checkBinary


def downsample(array, downsamplingSize, dim, weight=None):
    """
    Given an array, do downsampling along the dimension dim.
    """
    shape = list(array.shape)
    shape[dim] = -1
    shape.insert(dim+1, downsamplingSize)

    array = np.reshape(array, shape)
    if weight is None:
        weight = np.ones_like(array)
    else:
        weight = np.reshape(weight, shape)

    return np.average(array, axis=dim+1, weights=weight)

def xml2dataframe(filename):
    """
    Convert xml files to dataframe of pandas.
    """
    tree = ET.parse(filename)
    root = tree.getroot()
    
    df = [{} for _ in range(len(root))]
    for i, elem in enumerate(root):
        df[i] = elem.attrib
    df = pd.DataFrame(df)

    # Convert string to number if all the characters of the string are numeric characters.
    for c in df.columns:
        firstElement = df[c].iloc[0].replace(".", "", 1) # Remove the decimal point if exists.
        firstElement = firstElement.replace("-", "", 1) # Remove the negative sign if exists.
        if (type(firstElement) == str) and (str.isnumeric(firstElement)):
            df[c] = df[c].to_numpy(dtype=np.float32)

    return df

def convertTrajectoryFile(fileName):
    """
    Convert trajectory files generated by SUMO via updating column names and value units.
    """
    csv = pd.read_csv(fileName, sep=";")
    
    csv["link"] = csv["vehicle_lane"].apply(lambda x: x[:-2])
    csv["lane"] = csv["vehicle_lane"].apply(lambda x: x[-1])
    
    csv.rename(columns={
        "timestep_time": "time",
        "vehicle_id": "id",
        "vehicle_x": "x",
        "vehicle_y": "y",
        "vehicle_angle": "angle",
        "vehicle_speed": "speed.mph",
        "vehicle_pos": "pos",
        "vehicle_acceleration": "accel.fpss",
        "vehicle_type": "type"
    }, inplace=True)
    
    csv.drop("vehicle_slope", axis=1, inplace=True)
    
    csv["speed.mph"] = csv["speed.mph"] * 3.6 / 1.61 # meter per second to mile per hour
    csv["accel.fpss"] = csv["accel.fpss"] * 3.28 # meter per second to feet per second
    csv["x"] = csv["x"] * 3.28 # meter to feet
    csv["y"] = csv["y"] * 3.28 # meter to feet
    csv["pos"] = csv["pos"] * 3.28 # meter to feet
    return csv


# Initialize queue estimator.
estimator = qe.QueueEstimator("control_queueEstimator.json")

def estimateQueueLength(meteringUpdateFreq=30):
    """
    Combine basic safety messages and induction loop sensors to estimate queue length.
    """

    onrampNames = ["S26_O", "S30_O", "S33_O", "S35_O", "S40_O", "S45_O", "S48_O",
                   "S50_O", "S53_O", "S58_O", "S59_O", "S64_O", "S66_O"]

    bsm = []
    for b in bsmKeeper.BSMs:
        bsm.extend(b)
    for b in bsm:
        if ("car" in b["type"]) and ("cv" in b["type"]):
            b["type"] = "car_cv"        
        elif ("hov" in b["type"]) and ("cv" in b["type"]):
            b["type"] = "hov_cv"    
        elif ("lightTruck" in b["type"]) and ("cv" in b["type"]):
            b["type"] = "lightTruck_cv"
        elif ("heavyTruck" in b["type"]) and ("cv" in b["type"]):
            b["type"] = "heavyTruck_cv"

    flows = {"D18_O_0": 0, "D19_O_0": 0, "D20_O_0": 0, "D21_O_0": 0, "D22_O_0": 0,
             "D17_F_0": 0, "D18_F_0": 0, "D19_F_0": 0, "D20_F_0": 0}
        
    occs = {"D18_O_0": 0, "D19_O_0": 0, "D20_O_0": 0, "D21_O_0": 0, "D22_O_0": 0,
            "D17_F_0": 0, "D18_F_0": 0, "D19_F_0": 0, "D20_F_0": 0}
        
    for i in range(5, 23):
        f = mainlineInductionLoops["Mainline{}".format(i)].laneFlow
        o = mainlineInductionLoops["Mainline{}".format(i)].laneOcc
        for j in range(len(f)):
            flows["D{}_M_{}".format(i, j)] = f[j]
            occs["D{}_M_{}".format(i, j)] = o[j]
                
    for i in range(5, 18):
        f = onrampEntryInductionLoops["Onramp{}".format(i)].laneFlow
        o = onrampEntryInductionLoops["Onramp{}".format(i)].laneOcc
        if i == 16:
            flows["D16_O_entry_0".format(i, j)] = np.sum(f)
            occs["D16_O_entry_0".format(i, j)] = np.mean(o)
        else:
            for j in range(len(f)):
                flows["D{}_O_entry_{}".format(i, j)] = f[j]
                occs["D{}_O_entry_{}".format(i, j)] = o[j]
                
    for i in range(5, 18):
        f = onrampEntryInductionLoops["Onramp{}".format(i)].laneFlow
        o = onrampEntryInductionLoops["Onramp{}".format(i)].laneOcc

        for j in range(len(f)):
            flows["D{}_O_{}".format(i, j)] = f[j]
            occs["D{}_O_{}".format(i, j)] = o[j]
                
        
    for i in range(5, 17):
        f = offrampInductionLoops["Offramp{}".format(i)].laneFlow
        o = offrampInductionLoops["Offramp{}".format(i)].laneOcc

        for j in range(len(f)):
            flows["D{}_F_{}".format(i, j)] = f[j]
            occs["D{}_F_{}".format(i, j)] = o[j]
                
        
    prediction = estimator.process_time_step(bsm_dict=bsm, 
                                             occupancy_dict=occs, 
                                             flow_dict=flows, 
                                             timestep_start_seconds=env.step-meteringUpdateFreq)
        
    predictedQueue = [0 for _ in range(13)]
    for i in prediction:
        predictedQueue[onrampNames.index(i["ramp"])] += i["queue_count_max"]

    results = {}
    for i, j in enumerate(predictedQueue):
        results["Onramp{}".format(i+5)] = j

    return results    
             

def startSumo(simProjDir, simMainFile, simSeed, simMode="sumo", 
              isTrajSaved=False, trajFile=None, trajStartTime=None,
              isTripInfoSaved=False, tripInfoFile=None):
    """
    Initialize the simuation and return a connection with SUMO.
    """

    # Check whether output directory exists.
    if not os.path.exists(simProjDir+"Output"):
        os.mkdir(simProjDir+"Output")

    simSettings = [checkBinary(simMode), 
                   "-c", simProjDir+simMainFile, 
                   "--step-length", "1", 
                   "--seed", str(simSeed), 
                   "--no-warnings", "true", 
                   "--collision.action", "none"]
    
    if isTrajSaved:
        simSettings.extend(["--fcd-output", simProjDir+"Output/"+trajFile])
        simSettings.extend(["--fcd-output.acceleration", "true"])
        simSettings.extend(["--device.fcd.begin", str(trajStartTime)])
        
    if isTripInfoSaved:
        simSettings.extend(["--tripinfo-output", simProjDir+"Output/"+tripInfoFile])
        
    if simMode == "sumo-gui":
        simSettings.extend(["--start", "true"])
        simSettings.extend(["--quit-on-end", "true"])
    
    traci.start(simSettings)
    return traci.getConnection()


# Set simProjDir: simulation project directory
simProjDir = "./SUMO (few incidents)/"

# Set simMainFile: simulation main file
simMainFile = "I210.sumocfg.xml"

# Set random seed.
randomSeed = 5

# Initialize the simulation.
sumo = startSumo(simProjDir, simMainFile, randomSeed, "sumo", 
                 isTrajSaved=False, trajFile="traj.xml", trajStartTime=0,
                 isTripInfoSaved=True, tripInfoFile="tripInfo.xml")

# Wrap up SUMO.
env = fc.SimulationEnvironment(sumo)

# Initialize on-ramps.
onramps = {}
with open("onrampConfig.json") as f:
    configs = json.load(f)
    for onramp, config in configs.items():
        onramps[onramp] = {"numRampLane": config["numRampLane"], "maxVehNum": config["maxVehNum"]}

# Wrap up on-ramp entrance induction loop sensors.
onrampEntryInductionLoops = {}
with open("onrampEntryInductionLoopConfig.json") as f:
    configs = json.load(f)
    for onramp, config in configs.items():
        onrampEntryInductionLoops[onramp] = fc.InductionLoop(env, config["rampEntryInductionLoops"], config["period"])

# Wrap up on-ramp exit induction loop sensors.
onrampExitInductionLoops = {}
with open("onrampExitInductionLoopConfig.json") as f:
    configs = json.load(f)
    for onramp, config in configs.items():
        onrampExitInductionLoops[onramp] = fc.InductionLoop(env, config["rampExitInductionLoops"], config["period"]) 

# Wrap up on-ramp inction loop sensors for metering.
onrampMeteringInductionLoops = {}
with open("onrampMeteringInductionLoopConfig.json") as f:
    configs = json.load(f)
    for onramp, config in configs.items():
        onrampMeteringInductionLoops[onramp] = fc.InductionLoop(env, config["mainlineInductionLoops"], config["period"])

# Wrap up on-ramp lane area sensors.
onrampLaneAreaDetectors ={}
with open("onrampLaneAreaConfig.json") as f:
    configs = json.load(f)
    for onramp, config in configs.items():
        onrampLaneAreaDetectors[onramp] = fc.LaneAreaDetector(env, config["rampLaneAreaDetectors"], config["period"])

# Wrap up off-ramp induction loop sensors.
offrampInductionLoops = {}
with open("offrampInductionLoopConfig.json") as f:
    configs = json.load(f)
    for offramp, config in configs.items():
        offrampInductionLoops[offramp] = fc.InductionLoop(env, config["offrampInductionLoops"], config["period"])

# Wrap up mainline sensors.
mainlineInductionLoops = {}
with open("mainlineInductionLoopConfig.json") as f:
    configs = json.load(f)
    for mainline, config in configs.items():
        mainlineInductionLoops[mainline] = fc.InductionLoop(env, config["mainlineInductionLoops"], config["period"])

# Wrap up multi-entry-multi-exit sensors.
multiEntryMultiExitSensors = {}
with open("multiEntryMultiExitConfig.json") as f:
    configs = json.load(f)
    for i, config in configs.items():
        multiEntryMultiExitSensors[i] = fc.MultiEntryExitDetector(env, config["id"], config["period"])

# Wrap up meters and controllers.
meters, controllers = {}, {}
with open("meterConfig.json") as f:
    configs = json.load(f)
    for onramp, config in configs.items():
        meters[onramp] = fc.RampMeter(env, config["meterID"], [config["greenPhaseID"], config["redPhaseID"]], 
                                      numRampLane=onramps[onramp]["numRampLane"], 
                                      numVehPerGreenPerLane=config["numVehPerGreenPerLane"],
                                      greenPhaseLen=config["greenPhaseLen"], 
                                      redPhaseLen=config["minRedPhaseLen"], 
                                      minRedPhaseLen=config["minRedPhaseLen"], 
                                      maxRedPhaseLen=config["maxRedPhaseLen"])

        controllers[onramp] = fc.QueueInformedALINEA(env, meters[onramp], 
                                                     meteringUpdateFreq=30, 
                                                     gain=30,
                                                     allowedMaxRampQueueLen=0.8*onramps[onramp]["maxVehNum"],
                                                     mainlineSetpoint=15,
                                                     hasRampQueueOverride=True)

# Initialize ramp metering coordinator (HERO).
coordinator = fc.HeuristicRampMeteringCoordinator([controller for _, controller in controllers.items()] )


# Initialize BSM.
bsmKeeper = cv.BSMKeeper(env, obsPeriod=30)

# Set simulation time.
totalTime = 3600 * 7 # in unit of seconds.
meteringUpdateFreq = 30 # in unit of seconds.

startTimestamp = datetime.datetime.now()

for _ in range(totalTime):
    # Set metering rates from t to t+1.
    for _, controller in controllers.items():
        controller.meter.run()
    
    # Simulate from t to t+1.
    env.update()

    # Collect BSMs.
    bsmKeeper.collectBSM()

    # Update sensors.
    for _, sensor in onrampEntryInductionLoops.items():
        sensor.run()
    for _, sensor in onrampExitInductionLoops.items():
        sensor.run()
    for _, sensor in onrampMeteringInductionLoops.items():
        sensor.run()
    for _, sensor in onrampLaneAreaDetectors.items():
        sensor.run()
    for _, sensor in offrampInductionLoops.items():
        sensor.run()
    for _, sensor in mainlineInductionLoops.items():
        sensor.run()
    for _, sensor in multiEntryMultiExitSensors.items():
        sensor.run()
        
    # Update metering rate plan for each controlled on-ramp.
    if env.step % meteringUpdateFreq == 0:

        # Estimate queue length.
        estimatedQueue = estimateQueueLength()
        
        measurements = []
        for onramp, _ in controllers.items():
            measurements.append({"nearbyDownstreamMainlineOcc": onrampMeteringInductionLoops[onramp].edgeOcc,
                                 "rampQueueLen": estimatedQueue[onramp],
                                 "rampDemand": onrampEntryInductionLoops[onramp].edgeFlow,
                                 "rampQueueIndicator": onrampEntryInductionLoops[onramp].queueIndicator})
            
        coordinator.updateMeteringPlans(measurements)
        

        print("step: {}".format(env.step))
    
traci.close()

endTimestamp = datetime.datetime.now()
print("Simulation running time: {} seconds".format((endTimestamp-startTimestamp).seconds))

# Save measurements from SUMO API.
outputDir = simProjDir + "Output"

## Average travel time per vehicle (system) from 14:00-19:00.
# Sample every 900 seconds.
pd.DataFrame(multiEntryMultiExitSensors["System"].warehouse["meanTravelTime"][5:26]).to_csv(outputDir+"/AvgTravelTimePerVehicle_system.csv")

## Average travel time per vehicle (corridor/mainline) from 14:00-19:00.
# Sample every 900 seconds.
pd.DataFrame(multiEntryMultiExitSensors["Mainline"].warehouse["meanTravelTime"][5:26]).to_csv(outputDir+"/AvgTravelTimePerVehicle_corridor.csv")

## Average travel speed per vehicle (system) from 14:00-19:00.
# Sample every 900 seconds.
pd.DataFrame(downsample(np.array(multiEntryMultiExitSensors["System"].warehouse["vehSpeed"][2700:6*3600]), 900, dim=0)).to_csv(outputDir+"/AvgTravelSpeedPerVehicle_system.csv")

## Average travel speed per vehicle (corridor/mainline) from 14:00-19:00.
pd.DataFrame(downsample(np.array(multiEntryMultiExitSensors["Mainline"].warehouse["vehSpeed"][2700:6*3600]), 900, dim=0)).to_csv(outputDir+"/AvgTravelSpeedPerVehicle_corridor.csv")

## Average travel speed per vehicle (on-ramp) from 14:00-19:00.
avgTravelSpeedPerVehicle_onramp = []
for _, laneAreaDetector in onrampLaneAreaDetectors.items():
    # Sample every 900 seconds.
    avgTravelSpeedPerVehicle_onramp.append(downsample(np.array(laneAreaDetector.warehouse["vehSpeed"][2700:6*3600]), 900, dim=0))
avgTravelSpeedPerVehicle_onramp = np.mean(np.squeeze(avgTravelSpeedPerVehicle_onramp), axis=0)
pd.DataFrame(avgTravelSpeedPerVehicle_onramp).to_csv(outputDir+"/AvgTravelSpeedPerVehicle_onramp.csv")

## mainline speed from 14:00-19:00.
mainlineSpeed = []
for _, inductionLoop in mainlineInductionLoops.items():
    # Sample every 1800 seconds (60 30-seconds).
    mainlineSpeed.append(downsample(np.array(inductionLoop.warehouse["edgeSpeed"][60:720]), 60, 0))
pd.DataFrame(mainlineSpeed).to_csv(outputDir+"/SpeedMap.csv")

## Number of stops from 14:00-19:00.
pd.DataFrame(multiEntryMultiExitSensors["Mainline"].warehouse["numStop"][5:26]).to_csv(outputDir+"/NumStop_corridor.csv")

## Ramp saturation from 14:00-19:00.
rampSaturation = []
for onramp, laneAreaDetector in onrampLaneAreaDetectors.items():
    # Sample every 30 seconds.
    rampSaturation.append(downsample(np.array(onrampLaneAreaDetectors[onramp].warehouse["numVeh"][2700:6*3600]), 30, dim=0)/onramps[onramp]["maxVehNum"])
pd.DataFrame(rampSaturation).to_csv(outputDir+"/RampSaturation.csv")

## VMT/VHT from 14:00-19:00.
pd.DataFrame(downsample(np.array(multiEntryMultiExitSensors["System"].warehouse["vmtvht"][2700:6*3600]), 900, dim=0)).to_csv(outputDir+"/VMTVHT.csv")

## Throughput from 14:00-19:00.
outflow = []
for _, offrampInductionLoop in offrampInductionLoops.items():
    # Sample every 900 seocnds (30 30-second).
    outflow.append(downsample(np.array(offrampInductionLoop.warehouse["edgeFlow"][90:720]), 30, 0))
# Add the last mainline sensor.
outflow.append(downsample(np.array(mainlineInductionLoops["Mainline22"].warehouse["edgeFlow"][90:720]), 30, 0))
pd.DataFrame(np.sum(np.array(outflow), axis=0)).to_csv(outputDir+"/OutflowSum.csv")

# Move simulation results from the Output file to the file named by random seed.
newOutputDir = simProjDir + "Output_randomseed_{}".format(randomSeed)
if not os.path.exists(newOutputDir):
    os.mkdir(newOutputDir)
for file in os.listdir(outputDir):
    shutil.move(outputDir+"/"+file, newOutputDir+"/"+file)
