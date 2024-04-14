"""
File: order_management_interface.py
Author: Ankur Mahesh Chavan (achavan1@umd.edu),Datta Lohith Gannavarapu (gdatta@umd.edu),
Shail Kiritkumar Shah (sshah115@umd.edu) Vinay Krishna Bukka (vinay06@umd.edu),
Vishnu Mandala (vishnum@umd.edu)
Date: 03/28/2024
Description: Module to initiate order and manage orders based on priority for AGV guidance.
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from rclpy.qos import QoSProfile
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup,ReentrantCallbackGroup
from ariac_msgs.msg import Order as OrderMsg, AGVStatus, CompetitionState, AdvancedLogicalCameraImage
from ariac_msgs.srv import MoveAGV, SubmitOrder
from std_srvs.srv import Trigger
from queue import PriorityQueue
from geometry_msgs.msg import Pose
import time
import threading
import PyKDL


class Kitting:
    """
    Class to represent the kitting task of an order.
    """

    def __init__(self, order_data):
        self._agv_number = order_data.kitting_task.agv_number
        self._tray_id = order_data.kitting_task.tray_id
        self._parts = order_data.kitting_task.parts
        self._destination = order_data.kitting_task.destination

    @property
    def agv_number(self):
        return self._agv_number

    @property
    def tray_id(self):
        return self._tray_id

    @property
    def parts(self):
        return self._parts

    @property
    def destination(self):
        return self._destination


class Assembly:
    """
    Class to represent the assembly task of an order.
    """

    def __init__(self, order_data):
        self._agv_numbers = order_data.assembly_task.agv_numbers
        self._station = order_data.assembly_task.station
        self._parts = order_data.assembly_task.parts

    @property
    def agv_numbers(self):
        return self._agv_numbers

    @property
    def station(self):
        return self._station

    @property
    def parts(self):
        return self._parts


class CombinedTask:
    """
    Class to represent the combined task of an order.
    """

    def __init__(self, order_data):
        self._station = order_data.combined_task.station
        self._parts = order_data.combined_task.parts

    @property
    def station(self):
        return self._station

    @property
    def parts(self):
        return self._parts


class Order:
    """
    Class to represent an order.
    """

    def __init__(self, order_data):
        self._order_id = order_data.id
        self._order_type = order_data.type
        self._order_priority = order_data.priority

        self.waiting = (
            False  # Indicates if the order is currently in its waiting period
        )
        self.elapsed_wait = 0  # Track elapsed wait time for the order
        self.wait_start_time = None  # Track the start time of the wait period

        if self._order_type == OrderMsg.KITTING:
            self._order_task = Kitting(order_data)
        elif self._order_type == OrderMsg.ASSEMBLY:
            self._order_task = Assembly(order_data)
        elif self._order_type == OrderMsg.COMBINED:
            self._order_task = CombinedTask(order_data)


class OrderManagement(Node):
    """
    Class to manage the orders and competition state.

    Inherited Class:
        Node (rclpy.node.Node): Node class
    """

    def __init__(self, node_name):
        """
        Initialize the node.

        Args:
            node_name (str): Name of the node
        """
        super().__init__(node_name)
        self._order_callback_group = ReentrantCallbackGroup()
        self._sensor_callback_group = ReentrantCallbackGroup()
        self._competition_callback_group = ReentrantCallbackGroup()
        self._agv_callback_group = ReentrantCallbackGroup()
        self._service_group = ReentrantCallbackGroup()

        # Subscriptions
        qos_policy = rclpy.qos.QoSProfile(reliability=rclpy.qos.ReliabilityPolicy.BEST_EFFORT,
                                          history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                                          depth=1)

        self.orders_subscription = self.create_subscription(
            OrderMsg,
            "/ariac/orders",
            self._orders_initialization_cb,
            qos_profile=qos_policy,
            callback_group=self._order_callback_group
        )

        # self.competition_state_subscription = self.create_subscription(
        #     CompetitionState,
        #     "/ariac/competition_state",
        #     self._competition_state_cb,
        #     QoSProfile(depth=10),
        #     callback_group=self._competition_callback_group,
        # )

        self.left_table_camera_subscription = self.create_subscription(
            AdvancedLogicalCameraImage,
            "/ariac/sensors/left_table_camera/image",
            lambda msg: self._table_camera_callback(msg,'Left'),
            qos_profile=qos_policy,
            callback_group=self._sensor_callback_group,
        )
        
        self.right_table_camera_subscription = self.create_subscription(
            AdvancedLogicalCameraImage,
            "/ariac/sensors/right_table_camera/image",
            lambda msg: self._table_camera_callback(msg,'Right'),
            qos_profile=qos_policy,
            callback_group=self._sensor_callback_group,
        )
        
        self.left_bins_camera_subscription = self.create_subscription(
            AdvancedLogicalCameraImage,
            "/ariac/sensors/left_bins_camera/image",
            lambda msg: self._bin_camera_callback(msg,'Left'),
            qos_profile=qos_policy,
            callback_group=self._sensor_callback_group,
        )
        
        self.right_bins_camera_subscription = self.create_subscription(
            AdvancedLogicalCameraImage,
            "/ariac/sensors/right_bins_camera/image",
            lambda msg: self._bin_camera_callback(msg,'Right'),
            qos_profile=qos_policy,
            callback_group=self._sensor_callback_group,
        )
        
        # To prevent unused variable warning
        self.orders_subscription
        # self.competition_state_subscription
        self.left_table_camera_subscription
        self.right_table_camera_subscription
        self.left_bins_camera_subscription
        self.right_bins_camera_subscription
        
        # Initialize variables
        self.get_logger().info(f"Node {node_name} initialized")
        # self._orders_queue = PriorityQueue()
        self._agv_statuses = {}
        # self.current_order = None  # Track the currently processing or waiting order
        # self.competition_ended = False
        self.tables_done = {'Left':False,'Right':False}
        self.bins_done = {'Left':False,'Right':False}
        
        self._Tray_Dictionary = {}
        self._Bins_Dictionary = {}
        self._Parts_Dictionary={'colors':{0: 'Red', 1: 'Green', 2: 'Blue', 3: 'Orange', 4: 'Purple'},
                      'types':{10:'Battery', 11:'Pump', 12:'Sensor', 13:'Regulator'}}


        self._high_priority_orders = []
        self._normal_orders =[]
        self._time_reached_indices_high = []
        self._time_reached_indices_normal = []
        self._high_priority_orders_timer = [0]*10
        self._normal_orders_timer = [0]*10

        # self._order_priority_timer = self.create_timer(1, self.order_priority_timer_cb, callback_group=self._competition_callback_group)
        # self._order_processing_publisher = self.create_publisher(String,"/ariac_custom/order",10)
        # self._order_processing_subscription= self.create_subscription(String, "/ariac_custom/order", self._process_order,qos_profile=qos_policy,callback_group=self._order_callback_group)
        self._order_processing_flag = False


        self._agv1_status = self.create_subscription(
                AGVStatus,
                f"/ariac/agv1_status",
                lambda msg: self._agv1_status_cb(msg, 1),qos_profile=qos_policy,
                callback_group=self._order_callback_group,
            )
        self._agv2_status = self.create_subscription(
                AGVStatus,
                f"/ariac/agv2_status",
                lambda msg: self._agv2_status_cb(msg, 2),qos_profile=qos_policy,
                callback_group=self._order_callback_group,
            )
        self._agv3_status = self.create_subscription(
                AGVStatus,
                f"/ariac/agv3_status",
                lambda msg: self._agv3_status_cb(msg, 3),qos_profile=qos_policy,
                callback_group=self._order_callback_group,
            )
        self._agv4_status = self.create_subscription(
                AGVStatus,
                f"/ariac/agv4_status",
                lambda msg: self._agv4_status_cb(msg, 4),qos_profile=qos_policy,
                callback_group=self._order_callback_group,
            )        
        self._agv1_status_value = "Kitting Station"
        self._agv2_status_value = "Kitting Station"
        self._agv3_status_value = "Kitting Station"
        self._agv4_status_value = "Kitting Station"

    def _orders_initialization_cb(self, msg):
        """
        Callback for receiving orders.

        Args:
            msg (any): Message received from the topic
        """
        order = Order(msg)
        self.get_logger().info(
            f"Received Order: {str(order._order_id)} with priority: {int(order._order_priority)}"
        )
        
        if(order._order_priority):
            self._high_priority_orders.append(order)
            self._process_order(order)
        else:
            self._normal_orders.append(order)
            self._process_order(order)
        self.get_logger().info(f"HIgh,{self._high_priority_orders}")
        self.get_logger().info(f"Normal {self._normal_orders}")
        
        agv_id = order._order_task.agv_number
        # if agv_id not in self._agv_statuses:
        #     self.create_subscription(
        #         AGVStatus,
        #         f"/ariac/agv{agv_id}_status",
        #         lambda msg: self._agv_status_cb(msg, agv_id),
        #         callback_group=self._agv_callback_group,
        #     )
    # def order_priority_timer_cb(self):
    #     h_len = len(self._high_priority_orders)
    #     n_len = len(self._normal_orders)
        
    #     # self.get_logger().info(f"{self._time_reached_indices_high}---- {self._high_priority_orders} --- {self._high_priority_orders_timer}")
    #     # self.get_logger().info(f"{self._time_reached_indices_normal}---- {self._normal_orders} --- {self._normal_orders_timer} ")
    #     if(h_len > 0 and self._order_processing_flag==False):
    #         for i in range(h_len):
    #             self._high_priority_orders_timer[i] += 1
    #             if(self._high_priority_orders_timer[i] == 15):
    #                 self._time_reached_indices_high.append(i)
    #         if(len(self._time_reached_indices_high)>0):
    #             for i in self._time_reached_indices_high:
    #                 self._high_priority_orders_timer.pop(i)
    #                 # ord_to_process = self._high_priority_orders.pop(i)
    #                 ord_to_process = "h"+str(i)
    #                 self._string_msg = String()
    #                 self._string_msg.data = ord_to_process
    #                 self._order_processing_publisher.publish(self._string_msg)
    #             self._time_reached_indices_high =[]
    #     elif (n_len > 0 and self._order_processing_flag==False):
    #         for i in range(n_len):
    #             self._normal_orders_timer[i] += 1
    #             if(self._normal_orders_timer[i] == 15):
    #                 self._time_reached_indices_normal.append(i)
    #         if(len(self._time_reached_indices_normal)>0):
    #             for i in self._time_reached_indices_normal:
    #                 self._normal_orders_timer.pop(i)
    #                 ord_to_process = "n"+str(i)
    #                 self._string_msg = String()
    #                 self._string_msg.data = ord_to_process
    #                 self._order_processing_publisher.publish(self._string_msg)
    #                 # ord_to_process = self._normal_orders.pop(i)
    #             self._time_reached_indices_normal=[]
            
    #     self.get_logger().info(f"Returned {self._order_processing_flag}")

 

    # def _competition_state_cb(self, msg):
    #     """
    #     Callback for competition state changes. Starts the end condition checker when order announcements are done.
    #     """
    #     # Start the end condition checker when order announcements are done
    #     # self.get_logger().info(f"End wait")
    #     pass
        # if msg.competition_state == CompetitionState.ORDER_ANNOUNCEMENTS_DONE:
        #     if (
        #         self._end_condition_thread is None
        #         or not self._end_condition_thread.is_alive()
        #     ):
        #         self._end_condition_thread = threading.Thread(
        #             target=self._check_end_conditions
        #         )
        #         self._end_condition_thread.start()




    def _agv1_status_cb(self, msg, agv_id):
        """
        Callback for AGV status changes. Updates the AGV status in the dictionary.

        Args:
            msg (any): Message received from the topic
            agv_id (int): ID of the AGV
        """
        # Define a mapping for AGV locations
        location_status_map = {0: "Kitting Station", 3: "WAREHOUSE"}
        # self.get_logger().info(f" AGV Location Poses {msg.location} {msg.velocity} {self._agv_statuses}")
        status = location_status_map.get(msg.location, "OTHER")
        if(self._agv1_status_value!= status):
            self._agv1_status_value = status

    def _agv2_status_cb(self, msg, agv_id):
        """
        Callback for AGV status changes. Updates the AGV status in the dictionary.

        Args:
            msg (any): Message received from the topic
            agv_id (int): ID of the AGV
        """
        # Define a mapping for AGV locations
        location_status_map = {0: "Kitting Station", 3: "WAREHOUSE"}
        # self.get_logger().info(f" AGV Location Poses {msg.location} {msg.velocity} {self._agv_statuses}")
        status = location_status_map.get(msg.location, "OTHER")
        if(self._agv2_status_value!= status):
            self._agv2_status_value = status

    def _agv3_status_cb(self, msg, agv_id):
        """
        Callback for AGV status changes. Updates the AGV status in the dictionary.

        Args:
            msg (any): Message received from the topic
            agv_id (int): ID of the AGV
        """
        # Define a mapping for AGV locations
        location_status_map = {0: "Kitting Station", 3: "WAREHOUSE"}
        # self.get_logger().info(f" AGV Location Poses {msg.location} {msg.velocity} {self._agv_statuses}")
        status = location_status_map.get(msg.location, "OTHER")
        if(self._agv3_status_value!= status):
            self._agv3_status_value = status

    def _agv4_status_cb(self, msg, agv_id):
        """
        Callback for AGV status changes. Updates the AGV status in the dictionary.

        Args:
            msg (any): Message received from the topic
            agv_id (int): ID of the AGV
        """
        # Define a mapping for AGV locations
        location_status_map = {0: "Kitting Station", 3: "WAREHOUSE"}
        # self.get_logger().info(f" AGV Location Poses {msg.location} {msg.velocity} {self._agv_statuses}")
        status = location_status_map.get(msg.location, "OTHER")
        if(self._agv4_status_value!= status):
            self._agv4_status_value = status


    # def _agv_status_cb(self, msg, agv_id):
    #     """
    #     Callback for AGV status changes. Updates the AGV status in the dictionary.

    #     Args:
    #         msg (any): Message received from the topic
    #         agv_id (int): ID of the AGV
    #     """
    #     # Define a mapping for AGV locations
    #     location_status_map = {0: "Kitting Station", 3: "WAREHOUSE"}
    #     # self.get_logger().info(f" AGV Location Poses {msg.location} {msg.velocity} {self._agv_statuses}")
    #     status = location_status_map.get(msg.location, "OTHER")
    #     if(agv_id not in self._agv_statuses.keys()):
    #         self._agv_statuses[agv_id] = status
    #     if(self._agv_statuses[agv_id] != status):
    #         self._agv_statuses[agv_id] = status
    #         if(status == "WAREHOUSE"):
    #             if(agv_id==1 and self._agv1_status_value!= status):
    #                 self._agv1_status_value = "WAREHOUSE"
    #             elif(agv_id==2 and self._agv2_status_value!= status):
    #                 self._agv2_status_value = "WAREHOUSE"
    #             elif(agv_id==3 and self._agv3_status_value!= status):
    #                 self._agv3_status_value = "WAREHOUSE"
    #             elif(agv_id==4 and self._agv4_status_value!= status):
    #                 self._agv4_status_value = "WAREHOUSE"

    def _table_camera_callback(self, message, table_id='Unknown'):
        
        if table_id == 'Unknown':
            self.get_logger().warn("Unknown table ID")
            return
        
        if self.tables_done[table_id] == False:
            self.tables_done[table_id] = True
            self._Tray_Dictionary[table_id]={}
            tray_poses = message.tray_poses
            self.get_logger().info(f" Tray Poses {tray_poses}")
            if len(tray_poses) > 0:
                for tray in range(len(tray_poses)):
                    tray_pose_id = tray_poses[tray].id
                    # self.get_logger().info(f" Tray Poses ID {tray_pose_id}")
                    camera_pose = Pose()
                    camera_pose.position.x = message.sensor_pose.position.x
                    camera_pose.position.y = message.sensor_pose.position.y
                    camera_pose.position.z = message.sensor_pose.position.z
                    camera_pose.orientation.x = message.sensor_pose.orientation.x
                    camera_pose.orientation.y = message.sensor_pose.orientation.y
                    camera_pose.orientation.z = message.sensor_pose.orientation.z
                    camera_pose.orientation.w = message.sensor_pose.orientation.w

                    tray_pose = Pose()
                    tray_pose.position.x = tray_poses[tray].pose.position.x
                    tray_pose.position.y = tray_poses[tray].pose.position.y
                    tray_pose.position.z = tray_poses[tray].pose.position.z
                    tray_pose.orientation.x = tray_poses[tray].pose.orientation.x
                    tray_pose.orientation.y = tray_poses[tray].pose.orientation.y
                    tray_pose.orientation.z = tray_poses[tray].pose.orientation.z
                    tray_pose.orientation.w = tray_poses[tray].pose.orientation.w
                    tray_world_pose = self._multiply_pose(camera_pose, tray_pose)
                    if self._Tray_Dictionary[table_id] is None:
                        self._Tray_Dictionary[table_id] = {}
                    else:
                        self._Tray_Dictionary[table_id].update({tray_pose_id:{'position': [tray_world_pose.position.x,tray_world_pose.position.y,tray_world_pose.position.z], 'orientation': [tray_world_pose.orientation.x,tray_world_pose.orientation.y,tray_world_pose.orientation.z], 'status':False}})
                    # self.get_logger().info(f"    - {self._Tray_Dictionary}")

    def _bin_camera_callback(self, message,side='Unknown'):
        
        if side == 'Unknown':
            self.get_logger().warn("Unknown side ID")
            return
        
        if self.bins_done[side] == False:
            self.bins_done[side] = True
            bin_poses = message.part_poses
            bin_camera_pose = Pose()
            bin_camera_pose.position.x = message.sensor_pose.position.x
            bin_camera_pose.position.y = message.sensor_pose.position.y
            bin_camera_pose.position.z = message.sensor_pose.position.z
            bin_camera_pose.orientation.x = message.sensor_pose.orientation.x
            bin_camera_pose.orientation.y = message.sensor_pose.orientation.y
            bin_camera_pose.orientation.z = message.sensor_pose.orientation.z
            bin_camera_pose.orientation.w = message.sensor_pose.orientation.w

            self._Bins_Dictionary[side]={}
            for i in range(len(bin_poses)):
                bin_part = bin_poses[i].part
                bin_part_pose = Pose()
                bin_part_pose.position.x = bin_poses[i].pose.position.x
                bin_part_pose.position.y = bin_poses[i].pose.position.y
                bin_part_pose.position.z = bin_poses[i].pose.position.z
                bin_part_pose.orientation.x = bin_poses[i].pose.orientation.x
                bin_part_pose.orientation.y = bin_poses[i].pose.orientation.y
                bin_part_pose.orientation.z = bin_poses[i].pose.orientation.z
                bin_part_pose.orientation.w = bin_poses[i].pose.orientation.w

                bin_world_pose = self._multiply_pose(bin_camera_pose, bin_part_pose)
                type = self._Parts_Dictionary['types'][bin_part.type]
                color = self._Parts_Dictionary['colors'][bin_part.color]
                
                if (type,color) in self._Bins_Dictionary[side].keys():
                    keys=self._Bins_Dictionary[side][(type,color)].keys()
                    self._Bins_Dictionary[side][(type,color)][len(keys)]={'position': [bin_world_pose.position.x,bin_world_pose.position.y,bin_world_pose.position.z], 'orientation': [bin_world_pose.orientation.x,bin_world_pose.orientation.y,bin_world_pose.orientation.z],'picked': False}
                
                else:
                    self._Bins_Dictionary[side][(type,color)]={}
                    self._Bins_Dictionary[side][(type,color)][0]={'position': [bin_world_pose.position.x,bin_world_pose.position.y,bin_world_pose.position.z], 'orientation': [bin_world_pose.orientation.x,bin_world_pose.orientation.y,bin_world_pose.orientation.z],'picked': False}
                # self.get_logger().info(f"    - {self._Bins_Dictionary}")


    def _multiply_pose(self, pose1: Pose, pose2: Pose) -> Pose:
        '''
        Use KDL to multiply two poses together.
        Args:
            pose1 (Pose): Pose of the first frame
            pose2 (Pose): Pose of the second frame
        Returns:
            Pose: Pose of the resulting frame
        '''

        orientation1 = pose1.orientation
        frame1 = PyKDL.Frame(
            PyKDL.Rotation.Quaternion(orientation1.x, orientation1.y, orientation1.z, orientation1.w),
            PyKDL.Vector(pose1.position.x, pose1.position.y, pose1.position.z))

        orientation2 = pose2.orientation
        frame2 = PyKDL.Frame(
            PyKDL.Rotation.Quaternion(orientation2.x, orientation2.y, orientation2.z, orientation2.w),
            PyKDL.Vector(pose2.position.x, pose2.position.y, pose2.position.z))

        frame3 = frame1 * frame2

        # return the resulting pose from frame3
        pose = Pose()
        pose.position.x = frame3.p.x()
        pose.position.y = frame3.p.y()
        pose.position.z = frame3.p.z()

        q = frame3.M.GetRPY()
        pose.orientation.x = q[0]
        pose.orientation.y = q[1]
        pose.orientation.z = q[2]
        pose.orientation.w = 0.0

        return pose
    

    def _process_order(self, msg):
        """
        Process the order by locking the AGV tray, moving the AGV to the station, and submitting the order.

        Args:
            order (Order): Order object to process
        """
        # Process the order
        # order_priority_str,order_index = msg.data[0],int(msg.data[1])
        # if(order_priority_str=="h"):
        #     order = self._high_priority_orders.pop(order_index)
        # elif(order_priority_str=="n"):
        #     order = self._normal_orders.pop(order_index)

        order = msg
        self.get_logger().info(f"Processing order: {order._order_id}.")
        self._order_processing_flag = True
        self.get_logger().info("")
        self.get_logger().info("-"*50)
        stars=len(order._order_id) + 6
        self.get_logger().info("-" * ((50 - stars) // 2) + f"Order {order._order_id}" + "-" * ((50 - stars) // 2))
        self.get_logger().info("-"*50)

        # Get the tray pose and orientation
        tray_id = order._order_task.tray_id
        self.get_logger().info(f" - Tray Dict: {self._Tray_Dictionary}")
        # To get the Unuesd tray
        for key in self._Tray_Dictionary.keys():
            if tray_id not in self._Tray_Dictionary[key].keys():
                continue
            if self._Tray_Dictionary[key][tray_id]['status'] == False:
                table_id = key
                self._Tray_Dictionary[key][tray_id]['status'] = True
                break
        tray_pose = self._Tray_Dictionary[table_id][tray_id]['position']
        tray_orientation = self._Tray_Dictionary[table_id][tray_id]['orientation']
        self.get_logger().info("Kitting Tray:")
        self.get_logger().info(f" - ID: {tray_id}")
        self.get_logger().info(f" - Position (xyz): {tray_pose}")
        self.get_logger().info(f" - Orientation (rpy): {tray_orientation}")
        self.get_logger().info(f"Parts:")
        
        # # Get the parts and their poses
        parts = order._order_task.parts
        for part in parts:
            type = self._Parts_Dictionary['types'][part.part.type]
            color = self._Parts_Dictionary['colors'][part.part.color]
            final_part = None
            if len(self._Bins_Dictionary['Left'].items()) > 0:
                if (type, color) in self._Bins_Dictionary['Left'].keys():
                    for k, part_left in enumerate(self._Bins_Dictionary['Left'][(type, color)].values()):
                        if not part_left['picked']:
                            final_part = part_left
                            self._Bins_Dictionary['Left'][(type, color)][k]['picked'] = True
                            pose = part_left['position']
                            orientation = part_left['orientation']
                            break
            if len(self._Bins_Dictionary['Right'].items()) > 0 and final_part is None:
                if (type, color) in self._Bins_Dictionary['Right'].keys():
                    for k, part_right in enumerate(self._Bins_Dictionary['Right'][(type, color)].values()):
                        if not part_right['picked']:
                            final_part = part_right
                            self._Bins_Dictionary['Right'][(type, color)][k]['picked'] = True
                            pose = part_right['position']
                            orientation = part_right['orientation']
                            break
            else:
                self.get_logger().warn(f"No parts found in bins")
                return
            
            self.get_logger().info(f"    - {color} {type}")
            self.get_logger().info(f"       - Position (xyz): {pose}")
            self.get_logger().info(f"       - Orientation (rpy): {orientation}")
        
        self.get_logger().info("-"*50)
        self.get_logger().info("-"*50)
        self.get_logger().info("-"*50)
        
        
        agv_id = order._order_task.agv_number
        self._lock_tray(agv_id)
        self._move_agv(agv_id, order._order_task.destination)
        self._submit_order(agv_id, order._order_id)
        self._order_processing_flag = False
        self.get_logger().info(f"Order {order._order_id} processed and shipped.")
        self.get_logger().info(f"Process flag {self._order_processing_flag}")
    

    def _lock_tray(self, agv):
        """Function to lock the tray

        Args:
            agv (str): Name of the agv
        """
        # Lock the tray to AGV
        self.get_logger().info(f"Lock Tray service called")
        self._lock_trays_client = self.create_client(
            Trigger, f"/ariac/agv{agv}_lock_tray"
        )
        request = Trigger.Request()
        future = self._lock_trays_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)

        if future.result() is not None:
            response = future.result()
            if response:
                self.get_logger().info(f"AGV {agv} locked")
        else:
            self.get_logger().warn(f"Unable to lock AGV {agv}")

    def _move_agv(self, agv, destination):
        """Function to move the agv to the shipping station

        Args:
            agv (str): Name of the agv
            destination (str): Destination of the agv
        """
        # Move the AGV to the destination
        self.get_logger().info(f"Move AGV service called")
        self._move_agv_client = self.create_client(
            MoveAGV, f"/ariac/move_agv{agv}"
        )
        request = MoveAGV.Request()
        request.location = destination
        future = self._move_agv_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        destination = "Warehouse" if destination == 3 else destination
        if future.result() is not None:
            response = future.result()
            if response:
                self.get_logger().info(f"AGV: {agv} moved to {destination}")
        else:
            self.get_logger().warn(f"Service call failed {future.exception()}")
            self.get_logger().warn(f"Failed to move AGV: {agv} to {destination}")


    def _submit_order(self, agv_id, order_id):
        """
        Submit the order to the competition.

        Args:
            agv_id (int): ID of the AGV
            order_id (str): ID of the order
        """
        for i in range(50):
            pass
        
        self.get_logger().info(f"Submit Order service called")
        # self.get_logger().info(f"{self._agv_status_names[agv_id]}")
        # Wait until the AGV is in the warehouse

        # if (agv_id ==1):
        #     while self._agv1_status_value != "WAREHOUSE":
        #         self.get_logger().info(f"{agv_id} {self._agv1_status_value}")
        #         pass
        # elif (agv_id ==2):
        #     while self._agv2_status_value != "WAREHOUSE":
        #         self.get_logger().info(f"{agv_id} {self._agv2_status_value}")
        #         pass
        # elif (agv_id ==3):
        #     while self._agv3_status_value != "WAREHOUSE":
        #         self.get_logger().info(f"{agv_id} {self._agv3_status_value}")
        #         pass
        # elif (agv_id ==4):
        #     while self._agv4_status_value != "WAREHOUSE":
        #         self.get_logger().info(f"{agv_id} {self._agv4_status_value}")
        #         pass

        self._submit_order_client = self.create_client(
            SubmitOrder, "/ariac/submit_order"
        )
        request = SubmitOrder.Request()
        request.order_id = order_id
        future = self._submit_order_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        self.get_logger().info(f"After{self._agv_statuses}")
        if future.result() is not None:
            response = future.result()
            if response:
                self.get_logger().info(f"Order submitted")
        else:
            self.get_logger().warn(f"Unable to submit order")

    # def _check_end_conditions(self):
    #     """
    #     Periodically check if all orders are processed and AGVs are in warehouse, then end competition.
    #     """
    #     self.get_logger().info(
    #                 "Waiting"
    #             )
    #     while not self.competition_ended and rclpy.ok():
    #         if self._orders_queue.empty() and all(
    #             status == "WAREHOUSE" for status in self._agv_statuses.values()
    #         ):
    #             self.get_logger().info(
    #                 "All orders processed and AGVs at destination. Preparing to end competition."
    #             )
    #             self._end_competition()
    #         time.sleep(5)  # Check every 5 seconds

    # def _end_competition(self):
    #     """
    #     End the competition if all conditions are met.
    #     """
    #     if not self.competition_ended:
    #         self.competition_ended = True
    #         self.get_logger().info(f"End competition service called")
    #         self._end_competition_client = self.create_client(
    #             Trigger, "/ariac/end_competition"
    #         )
    #         request = Trigger.Request()
    #         future = self._end_competition_client.call_async(request)
    #         rclpy.spin_until_future_complete(self, future)

    #         if future.result() is not None:
    #             response = future.result()
    #             if response:
    #                 self.get_logger().info(f"Competition ended")
    #         else:
    #             self.get_logger().warn(f"Unable to end competition")
