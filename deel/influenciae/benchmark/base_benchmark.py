# Copyright IRT Antoine de Saint Exupéry et Université Paul Sabatier Toulouse III - All
# rights reserved. DEEL is a research program operated by IVADO, IRT Saint Exupéry,
# CRIAQ and ANITI - https://www.deel.ai/
# =====================================================================================
"""
Module defining some basic functionality for streamlining the process of benchmarking
the different influence calculation techniques. In particular, the main evaluation
task will be the detection of mislabeled examples (synthetically generated by adding
noise to labels in the training set) by looking at the most self-influential examples.
"""
from abc import abstractmethod
import os
import random
import json

import tensorflow as tf
import numpy as np
from tensorflow.keras.optimizers import Optimizer # pylint: disable=E0611

from .influence_factory import InfluenceCalculatorFactory
from ..types import Tuple, Dict, Any, Optional, List


class BaseTrainingProcedure:
    """
    A basic interface for neural network's learning procedure for streamlining batch
    training groups of models and facilitate the statistical test of the influence
    calculation's methods performances.
    """

    @abstractmethod
    def train(
            self,
            training_dataset: tf.data.Dataset,
            test_dataset: tf.data.Dataset,
            train_batch_size: int = 128,
            test_batch_size: int = 128,
            log_path: Optional[str] = None
    ) -> Tuple[float, float, tf.keras.Model, Any]:
        """
        Trains the model on the training dataset using a given size for the batches, and
        performs validation on the test dataset, eventually saving the model in the provided
        path (if not None).

        Parameters
        ----------
        training_dataset
            A TF dataset with all the data on which the model must be trained.
        test_dataset
            A TF dataset with all the data on which to validate the model's performance.
        train_batch_size
            An integer with the size of the batches on which to train the model.
        test_batch_size
            An integer with the size of the batches on which to perform the validation.
        log_path
            An (optional) string specifying where to save the model (if desired).

        Returns
        -------
        A tuple with: (the train accuracy, the test accuracy, the final model, the information from the model saver)
        """
        raise NotImplementedError


class MislabelingDetectorEvaluator:
    """
    A class implementing a benchmarking pipeline for influence calculators based on their capacity
    to point out the most self-influential examples as being the (voluntarily added) mislabeled
    examples in the training dataset.

    Notes
    -----
    As such, the experiments will consist on training a model using the specified procedure on a
    noisy dataset, computing the self-influence of each of the training dataset's points, sorting
    them by it and seeing how fast we can find all the mislabeled examples. In an industrial setting,
    the derivative of this curve close to zero will be paramount, as this means that we can find a
    considerable percentage of these samples by looking at a low percentage of the dataset (and thus,
    be done by a human operator).

    Parameters
    ----------
    training_dataset
            A TF dataset with all the data on which the model must be trained.
    test_dataset
        A TF dataset with all the data on which to validate the model's performance.
    training_procedure
        An object implementing the BaseTrainingProcedure interface and describing how the model
        must be trained.
    nb_classes
        An integer with the amount of classes of the classification problem.
    mislabeling_ratio
        A float with the ratio of noise to add to the training dataset's labels (ranging from 0. to 1.).
    train_batch_size
        An integer with the size of the batches on which to train the model.
    test_batch_size
        An integer with the size of the batches on which to perform the validation.
    config
        A dictionary with the configuration to save it alongside the results for traceability.
    """

    def __init__(
            self,
            training_dataset: tf.data.Dataset,
            test_dataset: tf.data.Dataset,
            training_procedure: BaseTrainingProcedure,
            nb_classes: int,
            mislabeling_ratio: float,
            train_batch_size: int = 128,
            test_batch_size: int = 128,
            influence_batch_size: Optional[int] = None,
            config: Optional[Dict] = None) -> None:
        assert 0. < mislabeling_ratio < 1.
        self.training_dataset = training_dataset
        self.train_batch_size = train_batch_size
        self.test_dataset = test_dataset
        self.test_batch_size = test_batch_size
        self.training_procedure = training_procedure
        self.nb_classes = nb_classes
        self.mislabeling_ratio = mislabeling_ratio
        if influence_batch_size is None:
            self.influence_batch_size = self.train_batch_size
        else:
            self.influence_batch_size = influence_batch_size

        if config is None:
            self.config = {}
        else:
            self.config = config

    def bench(
            self,
            influence_calculator_factories: Dict[str, InfluenceCalculatorFactory],
            nbr_of_evaluation: int,
            path_to_save: str,
            seed: int = 0,
            verbose: bool = True,
            use_tensorboard: bool = False
    ) -> Dict[str, Tuple[np.array, np.array, float]]:
        """
        Performs the whole benchmark for a group of influence calculator techniques and a number of
        evaluations for each of them (for statistical significance).

        Parameters
        ----------
        influence_calculator_factories
            A dictionary with the name of the influence calculator technique and a factory for creating
            instances for them.
        nbr_of_evaluation
            An integer with the amount of evaluations per method.
        path_to_save
            A string specifying the path to save the results.
        seed
            An integer for setting the seed on all the random number generators.
        verbose
            A boolean indicating whether progress is reported in stdout or not.
        use_tensorboard
            A boolean indicating if the results are to be progressively logged into tensorboard.

        Returns
        -------
        result
            A dictionary with the name of each method and its results.
        """
        result = {}
        for name, influence_calculator_factory in influence_calculator_factories.items():
            if verbose:
                print("starting to evaluate=" + str(name))

            curves, mean_curve, roc = self.evaluate(influence_calculator_factory, nbr_of_evaluation, seed, verbose,
                                                    path_to_save, use_tensorboard, name)

            result[name] = (curves, mean_curve, roc)

            if verbose:
                print(name + " | mean roc=" + str(roc))

        return result

    def evaluate(
            self,
            influence_factory: InfluenceCalculatorFactory,
            nbr_of_evaluation: int,
            seed: int = 0,
            verbose: bool = True,
            path_to_save: Optional[str] = None,
            use_tensorboard: bool = False,
            method_name: Optional[str] = None
    ) -> Tuple[np.array, np.array, float]:
        """
        Performs one benchmark evaluation over one influence calculator technique the specified number
        of times (for statistical significance).

        Parameters
        ----------
        influence_factory
            A factory for instantiating objects for a given influence calculator technique.
        nbr_of_evaluation
            An integer with the amount of evaluations per method.
        seed
            An integer for setting the seed on all the random number generators.
        verbose
            A boolean indicating whether progress is reported in stdout or not.
        path_to_save
            A string specifying the path to save the results.
        use_tensorboard
            A boolean indicating if the results are to be progressively logged into tensorboard.
        method_name
            An optional string with the experience's name.

        Returns
        -------
        curves, mean_curve, roc
            A tuple with the experience's results: (each of the individual curves, the mean curve, the ROC)
        """
        curves = []

        if use_tensorboard and (path_to_save is None):
            path_to_save = "./"

        if path_to_save is not None:
            dirname = path_to_save + "/" + method_name
            if not os.path.exists(dirname):
                os.makedirs(dirname)
            with open(dirname + "/config.json", 'w', encoding="utf-8") as fp:
                json.dump(self.config, fp, indent=4)

        if method_name is None:
            method_name = 'experiment'

        for index in range(nbr_of_evaluation):

            if use_tensorboard:
                experiment_name = method_name + "_" + str(index)

                file_writer = tf.summary.create_file_writer(path_to_save + "/" + method_name + "/seed" + str(index),
                                                            filename_suffix=experiment_name)
                tf_writer = file_writer.as_default()

            tf.keras.backend.clear_session()
            self.set_seed(seed + index)

            noisy_training_dataset, noisy_label_indexes = self.build_noisy_training_dataset()
            noisy_label_indexes = noisy_label_indexes[0]

            acc_train, acc_test, model, data_train = self.training_procedure.train(
                noisy_training_dataset,
                self.test_dataset,
                self.train_batch_size,
                self.test_batch_size,
                log_path=None if path_to_save is None else path_to_save + "/" + method_name + "/seed" + str(index))

            influence_calculator = influence_factory.build(
                noisy_training_dataset.shuffle(1000).batch(self.influence_batch_size), model, data_train)

            influences_values = influence_calculator._compute_influence_values(  # pylint: disable=W0212
                noisy_training_dataset.batch(self.influence_batch_size))

            # compute curve and indexes
            sorted_influences_indexes = np.argsort(-np.squeeze(influences_values))

            sorted_curve = self.__compute_curve(sorted_influences_indexes, noisy_label_indexes)
            curves.append(sorted_curve)

            roc = self._compute_roc(sorted_curve)
            if verbose:
                print("seed nbr=" + str(index) + " | acc train=" + str(acc_train) + " | acc test=" + str(
                    acc_test) + " | roc=" + str(roc))

            if use_tensorboard:
                with tf_writer:
                    tf.summary.scalar("roc_value", roc, index)
                    self.plot_tensorboard_roc(sorted_curve, "roc_curve")

            if path_to_save is not None:
                curves_, mean_curve_, roc_ = self.__build(curves)
                self.__save(path_to_save + "/" + method_name + "/data.npy", curves_, mean_curve_, roc_)

        curves, mean_curve, roc = self.__build(curves)

        if use_tensorboard:
            file_writer = tf.summary.create_file_writer(path_to_save + "/synthesis/" + method_name + "/")
            with file_writer.as_default():
                tf.summary.scalar("roc_mean", roc, 0)
                tf.summary.scalar("roc_mean", roc, 1)
                self.plot_tensorboard_roc(mean_curve, "roc_curve_mean")

        return curves, mean_curve, roc

    @staticmethod
    def plot_tensorboard_roc(curve: np.ndarray, experiment_name: str):
        """
        Plots a mislabeled samples detection ROC curve on tensorboard.

        Parameters
        ----------
        curve
            A numpy array with the experiment's curve
        experiment_name
            A string with the experiment's name
        """
        for i, c in enumerate(curve):
            tf.summary.scalar(experiment_name, c, i)

    def __build(self, curves: List[np.ndarray]) -> Tuple[np.ndarray, np.ndarray, float]:
        """
        Formats the curves, computes the mean curve and the ROC.

        Parameters
        ----------
        curves
            A list with the experiment's curves mislabeled sample detection curves.
        Returns
        -------
        curves, mean_curve, roc
            A tuple with: (curves in numpy array format, the mean curve, the roc value)
        """
        curves = np.asarray(curves)
        mean_curve = np.mean(curves, axis=0)
        roc = self._compute_roc(mean_curve)

        return curves, mean_curve, roc

    @staticmethod
    def _compute_roc(curve: np.array) -> float:
        """
        Computes the ROC value of the curve.

        Parameters
        ----------
        curve
            A numpy array with a curve.

        Returns
        -------
        roc
            The roc value.
        """
        roc = np.mean(curve)
        return roc

    @staticmethod
    def set_seed(seed: int):
        """
        Sets all the random seeds on TensorFlow, numpy and python for traceability.

        Parameters
        ----------
        seed
            An integer with the seed value.
        """
        tf.random.set_seed(seed)
        np.random.seed(seed)
        random.seed(seed)

    @staticmethod
    def __compute_curve(sorted_influences_indexes: np.ndarray, noisy_label_indexes: np.ndarray) -> np.array:
        """
        Computes the mislabeled sample detection curve using the indices of the samples sorted
        by their self-influence and the ground-truth indices of the target points.

        Parameters
        ----------
        sorted_influences_indexes
            A numpy array with the sample's indices sorted by their self-influence.
        noisy_label_indexes
            A numpy array with the dataset's mislabeled examples' ground-truth.

        Returns
        -------
        curve
            A numpy array with the detection curve as we progressively scan the dataset.
        """
        index = np.in1d(sorted_influences_indexes, noisy_label_indexes)
        index = tf.cast(index, dtypes=np.int32)
        curve = np.cumsum(index)
        if curve[-1] != 0:
            curve = curve / curve[-1]

        return curve

    def build_noisy_training_dataset(self) -> Tuple[tf.data.Dataset, np.array]:
        """
        Generates a noisy version of the object's own dataset. In particular, it will include noise
        in the label information (i.e. the label will be switched at random). More noise will
        effectively impact the model's capacity to predict correctly on the test set, as it will
        need to learn to use spurious correlations to attain the 100% accuracy on the training
        dataset.

        Returns
        -------
        noisy_dataset, noise_indexes
            A tuple with the noisy dataset and a numpy array with the flipped labels (used for validation
            during the evaluation).
        """
        dataset_size = tf.data.experimental.cardinality(self.training_dataset)
        noise_mask = np.random.uniform(size=(dataset_size,)) > self.mislabeling_ratio
        noise_mask_dataset = tf.data.Dataset.from_tensor_slices(noise_mask)
        noisy_dataset = tf.data.Dataset.zip((self.training_dataset, noise_mask_dataset))

        def noise_map(z, y_mask):
            """
            Flips a single sample's labels following the mask.
            """
            (x, y) = z
            y_noise = tf.random.uniform(shape=(1,)) * tf.cast((tf.shape(y)[-1] - 1), dtype=tf.float32)
            y_noise = tf.cond(y_noise > tf.cast(tf.argmax(y), dtype=tf.float32), lambda: y_noise + 1, lambda: y_noise)
            y_noise = tf.cast(y_noise, dtype=tf.int32)
            y_noise = tf.cast(tf.squeeze(tf.one_hot(y_noise, tf.shape(y)[-1]), axis=0), dtype=y.dtype)
            y = tf.where(y_mask, y, y_noise)
            return x, y

        noisy_dataset = noisy_dataset.map(noise_map)

        noise_indexes = np.where(np.logical_not(noise_mask))
        return noisy_dataset, noise_indexes

    @staticmethod
    def __save(path_to_save: str, curves: np.array, mean_curve: np.array, roc: float) -> None:
        """
        Saves an evaluation's results to the disk.

        Parameters
        ----------
        path_to_save
            A string with the path into which to save the results.
        curves
            A numpy array with the different detection curves.
        mean_curve
            A numpy array with the mean detection curve.
        roc
            A float with the ROC value.
        """
        dirname = os.path.dirname(path_to_save)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        np.save(path_to_save, np.array((curves, mean_curve, roc), dtype=object), allow_pickle=True)


class ModelsSaver(tf.keras.callbacks.Callback):
    """
    A simple class to save models after optimizer updates. It will prove itself useful for tracing the
    different information to be able to use the TracIn method.

    Parameters
    ----------
    epochs_to_save
        A list of integers indicating on which epochs to save a model's checkpoint.
    optimizer
        The model's optimizer.
    saving_path
        An (optional) string for saving the results on the disk.
    """

    def __init__(self, epochs_to_save: List[int], optimizer: Optimizer, saving_path: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.epochs_to_save = epochs_to_save
        self.optimizer = optimizer

        self.models = []
        self.learning_rates = []

        if saving_path is not None and not os.path.exists(saving_path):
            os.mkdir(saving_path)
        self.saving_path = saving_path

    def on_epoch_end(self, epoch: int, logs: Optional[Dict] = None) -> None:
        """
        Save the relevant training information (model, learning rate, save to disk if desired)
        after a training epoch.

        Parameters
        ----------
        epoch
            An integer with the current epoch. If it is in the list of epochs after which to
            save the training information, do so.
        logs
            Dict, metric results for this training epoch, and for the validation epoch if validation
            is performed. Validation result keys are prefixed with val_. For training epoch, the
            values of the Model's metrics are returned.
        """
        if epoch in self.epochs_to_save:
            epoch_model = tf.keras.models.clone_model(self.model)
            epoch_model.build(self.model.input_shape)
            epoch_model.set_weights(self.model.get_weights())

            epoch_lr = self.optimizer.lr
            self.models.append(epoch_model)
            self.learning_rates.append(epoch_lr.numpy())

            if self.saving_path is not None:
                tf.data.experimental.save(f"{self.saving_path}/model_ep_{epoch:.6d}")
                np.save(f"{self.saving_path}/learning_rates", np.array(self.learning_rates), allow_pickle=True)
                with open(f"{self.saving_path}/logs.json", "w", encoding='utf8') as f:
                    json.dump(logs, f)
