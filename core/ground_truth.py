import tensorflow as tf
import numpy as np

from utils.IoU import IOU
from utils.tools import str_to_int, resize_box, preprocess_image
from configuration import MAX_BOXES_PER_IMAGE, IMAGE_WIDTH, IMAGE_HEIGHT, IOU_THRESHOLD
from core.anchor import DefaultBoxes


class ReadDataset(object):
    def __init__(self):
        pass

    @staticmethod
    def __get_image_information(single_line):
        line_string = bytes.decode(single_line.numpy(), encoding="utf-8")
        line_list = line_string.strip().split(" ")
        image_file, image_height, image_width = line_list[:3]
        image_height, image_width = str_to_int(image_height), str_to_int(image_width)
        boxes = []
        num_of_boxes = (len(line_list) - 3) / 5
        if int(num_of_boxes) == num_of_boxes:
            num_of_boxes = int(num_of_boxes)
        else:
            raise ValueError("num_of_boxes must be 'int'.")
        for index in range(num_of_boxes):
            if index < MAX_BOXES_PER_IMAGE:
                xmin = str_to_int(line_list[3 + index * 5])
                ymin = str_to_int(line_list[3 + index * 5 + 1])
                xmax = str_to_int(line_list[3 + index * 5 + 2])
                ymax = str_to_int(line_list[3 + index * 5 + 3])
                class_id = int(line_list[3 + index * 5 + 4])
                xmin, ymin, xmax, ymax = resize_box(image_height, image_width, xmin, ymin, xmax, ymax)
                boxes.append([xmin, ymin, xmax, ymax, class_id])
        num_padding_boxes = MAX_BOXES_PER_IMAGE - num_of_boxes
        if num_padding_boxes > 0:
            for i in range(num_padding_boxes):
                boxes.append([0, 0, 0, 0, -1])
        boxes_array = np.array(boxes, dtype=np.float32)  # shape: (MAX_BOXES_PER_IMAGE, 5)
        return image_file, boxes_array

    def read(self, batch_data):
        image_file_list = []
        boxes_list = []
        for item in range(batch_data.shape[0]):
            image_file, boxes = self.__get_image_information(single_line=batch_data[item])
            image_file_list.append(image_file)
            boxes_list.append(boxes)
        boxes = np.stack(boxes_list, axis=0)   # shape : (batch_size, MAX_BOXES_PER_IMAGE, 5)
        image_list = []
        for item in image_file_list:
            image_tensor = preprocess_image(img_path=item)
            image_list.append(image_tensor)
        images = tf.stack(values=image_list, axis=0)
        return images, boxes


class MakeGT(object):
    def __init__(self, batch_data, output_features):
        self.batch_data = batch_data
        self.batch_size = batch_data.shape[0]
        self.num_predict_features = 6
        self.read_dataset = ReadDataset()
        self.default_boxes = DefaultBoxes(feature_map_list=output_features)
        self.iou_threshold = IOU_THRESHOLD

        self.images, self.boxes = self.read_dataset.read(self.batch_data)
        self.predict_boxes = self.default_boxes.generate_default_boxes()

    def ___transform_true_boxes(self):
        boxes_xywhc = self.__to_xywhc(self.boxes)
        true_boxes_x = boxes_xywhc[..., 0] / IMAGE_WIDTH
        true_boxes_y = boxes_xywhc[..., 1] / IMAGE_HEIGHT
        true_boxes_w = boxes_xywhc[..., 2] / IMAGE_WIDTH
        true_boxes_h = boxes_xywhc[..., 3] / IMAGE_HEIGHT
        true_boxes_c = boxes_xywhc[..., -1]
        true_boxes = np.stack((true_boxes_x, true_boxes_y, true_boxes_w, true_boxes_h, true_boxes_c), axis=-1)
        return true_boxes

    @staticmethod
    def __to_xywhc(boxes):
        xy = 0.5 * (boxes[..., 0:2] + boxes[..., 2:4])
        wh = boxes[..., 2:4] - boxes[..., 0:2]
        c = boxes[..., -1:]
        return np.concatenate((xy, wh, c), axis=-1)  # (center_x, center_y, w, h, c)

    @staticmethod
    def __get_valid_boxes(boxes):
        num_of_boxes = boxes.shape[0]
        valid_boxes = []
        for i in range(num_of_boxes):
            if boxes[i, -1] > 0.0:
                valid_boxes.append(boxes[i])
        valid_boxes = np.array(valid_boxes, dtype=np.float32)
        return valid_boxes

    def __label_positive_and_negative_predicted_boxes(self, box_true, box_pred):
        box_true_coord = box_true[..., :4]
        box_true_class = box_true[..., -1]
        iou_outside = []
        for i in range(box_true_coord.shape[0]):
            iou_inside = []
            for j in range(box_pred.shape[0]):
                iou = IOU(box_1=box_true_coord[i], box_2=box_pred[j]).calculate_iou()
                iou_inside.append(iou)
            iou_outside.append(iou_inside)
        iou_array = np.array(iou_outside, dtype=np.float32)  # shape: (num_of_true_boxes, total_num_of_default_boxes)
        iou_max = np.max(iou_array, axis=0)
        max_index = np.argmax(iou_array, axis=0)
        max_index_class = np.zeros_like(max_index, dtype=np.float32)
        for k in range(max_index.shape[0]):
            max_index_class[k] = box_true_class[max_index[k]]
        pos_boolean = np.where(iou_max > self.iou_threshold, 1, 0)  # 1 for positive, 0 for negative
        pos_class_index = max_index_class * pos_boolean
        pos_class_index = pos_class_index.reshape((-1, 1))
        labeled_box_pred = np.concatenate((box_pred, pos_class_index), axis=-1)
        return labeled_box_pred

    def generate_pred_boxes(self):
        true_boxes = self.___transform_true_boxes()  # shape: (batch_size, MAX_BOXES_PER_IMAGE, 5)
        pred_boxes_list = []
        for n in range(self.batch_size):
            # shape : (N, 5), where N is the number of valid true boxes for each input image.
            valid_true_boxes = self.__get_valid_boxes(true_boxes[n])
            pred_boxes = self.__label_positive_and_negative_predicted_boxes(valid_true_boxes, self.predict_boxes)
            pred_boxes_list.append(pred_boxes)
        batch_pred_boxes = np.stack(pred_boxes_list, axis=0)   # shape: (batch_size, total_num_of_default_boxes, 5)
        return true_boxes, batch_pred_boxes