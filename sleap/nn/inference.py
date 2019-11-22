import argparse
import attr
import os
import numpy as np
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sleap import Labels, LabeledFrame, util
from sleap.nn import job
from sleap.nn import model
from sleap.nn import utils

from sleap.nn import region_proposal
from sleap.nn import peak_finding
from sleap.nn import paf_grouping
from sleap.nn import topdown
from sleap.nn import tracking

POLICY_CLASSES = dict(
    centroid=region_proposal.CentroidPredictor,  # requires model
    region=region_proposal.RegionProposalExtractor,  # no model
    topdown=topdown.TopDownPeakFinder,  # requires model
    confmap=peak_finding.ConfmapPeakFinder,  # requires model
    paf=paf_grouping.PAFGrouper,  # requires model
)


@attr.s(auto_attribs=True)
class Predictor:
    policies: Dict[str, object]

    _tracker_takes_img: bool = False

    def __attrs_post_init__(self):
        import inspect

        if "tracking" in self.policies:
            function_sig = inspect.signature(self.policies["tracking"].track)
            self._tracker_takes_img = "img" in function_sig.parameters

    def predict(
        self, video_filename: str, frames: Optional[List[int]] = None
    ) -> List[LabeledFrame]:
        """Runs entire inference pipeline on frames from a video file."""

        video_ds = utils.VideoLoader(filename=video_filename, frame_inds=frames,)

        predicted_frames = []

        for chunk_ind, frame_inds, imgs in video_ds:
            predicted_instances_chunk = self.predict_chunk(
                imgs, chunk_ind, video_ds.chunk_size
            )

            self.track_chunk(predicted_instances_chunk, frame_inds, imgs)

            frames = self.make_labeled_frames(
                predicted_instances_chunk, frame_inds, video_ds.video
            )

            predicted_frames.extend(frames)

        return predicted_frames

    def predict_chunk(self, img_chunk, chunk_ind, chunk_size):
        """Runs the inference components of pipeline for a chunk."""

        centroid_predictor = self.policies["centroid"]
        region_proposal_extractor = self.policies["region"]

        centroids, centroid_vals = centroid_predictor.predict(img_chunk)

        region_proposal_sets = region_proposal_extractor.extract(
            img_chunk, centroids, centroid_vals
        )
        for region_ind in range(len(region_proposal_sets)):
            region_proposal_sets[region_ind].sample_inds += chunk_ind * chunk_size

        if "topdown" in self.policies:
            topdown_peak_finder = self.policies["topdown"]

            rps = region_proposal_sets[0]

            sample_peak_pts, sample_peak_vals = topdown_peak_finder.predict_rps(rps)
            sample_peak_pts = sample_peak_pts.to_tensor().numpy()
            sample_peak_vals = sample_peak_vals.to_tensor().numpy()

            predicted_instances_chunk = topdown.make_sample_grouped_predicted_instances(
                sample_peak_pts,
                sample_peak_vals,
                np.unique(rps.sample_inds),
                topdown_peak_finder.inference_model.skeleton,
            )

        elif "confmap" in self.policies and "paf" in self.policies:
            confmap_peak_finder = self.policies["confmap"]
            paf_grouper = self.policies["paf"]

            region_peak_sets = []
            for rps in region_proposal_sets:
                region_peaks = confmap_peak_finder.predict_rps(rps)
                region_peak_sets.append(region_peaks)

            region_instance_sets = []
            for rps, region_peaks in zip(region_proposal_sets, region_peak_sets):
                region_instances = paf_grouper.predict_rps(rps, region_peaks)
                region_instance_sets.append(region_instances)

            predicted_instances_chunk = defaultdict(list)
            for region_instance_set in region_instance_sets:
                for sample, region_instances in region_instance_set.items():
                    predicted_instances_chunk[sample].extend(region_instances)

        return predicted_instances_chunk

    def track_chunk(self, predicted_instances_chunk, frame_inds, img_chunk):
        """Runs tracker for each frame in chunk."""
        for (sample_idx, instances), frame_idx, img in zip(
            predicted_instances_chunk.items(), frame_inds, img_chunk
        ):
            predicted_instances_chunk[sample_idx] = self.track_next_sample(
                untracked_instances=instances, t=frame_idx, img=img,
            )

    def track_next_sample(
        self, untracked_instances: List["PredictedInstance"], t: int = None, img=None
    ) -> List["PredictedInstance"]:
        """Runs tracker for a single frame."""
        if "tracking" not in self.policies:
            return untracked_instances

        tracker = self.policies["tracking"]

        track_args = dict(untracked_instances=untracked_instances, t=t)
        if self._tracker_takes_img:
            track_args["img"] = img
        else:
            track_args["img"] = None

        return tracker.track(**track_args)

    def make_labeled_frames(
        self, instances_chunk, frame_inds, video
    ) -> List[LabeledFrame]:
        """Makes LabeledFrame objects for all predictions in chunk."""
        frames = [
            LabeledFrame(frame_idx=frame_idx, instances=instances, video=video)
            for frame_idx, instances in zip(frame_inds, instances_chunk.values())
            if instances
        ]
        return frames

    @classmethod
    def from_cli_args(cls):
        parser = cls.make_cli_parser()
        args = parser.parse_args()
        policies = cls.cli_args_to_policies(args)

        cls.check_valid_policies(policies)

        return cls(policies=policies), args

    @classmethod
    def make_cli_parser(cls):

        # Helper functions for building parser
        def add_class_args(parser, attrs_class, arg_scope: str, exclude_args):
            def is_arg_to_include(arg_name: str):
                if arg_name.startswith("_"):
                    return False
                if arg_name.endswith("_model"):
                    return False
                if exclude_args is not None and arg_scope in exclude_args:
                    if arg_name in exclude_args[arg_scope]:
                        return False
                return True

            def arg_docstring(attrs_class, arg_name):
                # TODO: parse docstring and return text for this attribute
                return ""

            for attrib in attr.fields(attrs_class):
                if is_arg_to_include(attrib.name):
                    help_string = arg_docstring(attrs_class, attrib.name)
                    if attrib.default is not attr.NOTHING:
                        help_string += f" (default: {attrib.default})"
                    parser.add_argument(
                        f"--{arg_scope}.{attrib.name}",
                        type=attrib.type,
                        help=help_string,
                    )

        def frame_list(frame_str: str):

            # Handle ranges of frames. Must be of the form "1-200"
            if "-" in frame_str:
                min_max = frame_str.split("-")
                min_frame = int(min_max[0])
                max_frame = int(min_max[1])
                return list(range(min_frame, max_frame + 1))

            return [int(x) for x in frame_str.split(",")] if len(frame_str) else None

        # Make the parser
        parser = argparse.ArgumentParser()

        # Add args for entire pipeline
        parser.add_argument("data_path", help="Path to video file")
        parser.add_argument(
            "-m",
            "--model",
            dest="models",
            action="append",
            help="Path to saved model (confmaps, pafs, ...) JSON. "
            "Multiple models can be specified, each preceded by --model.",
            required=True,
        )

        parser.add_argument(
            "--frames",
            type=frame_list,
            default="",
            help="List of frames to predict. Either comma separated list (e.g. 1,2,3) or "
            "a range separated by hyphen (e.g. 1-3). (default is entire video)",
        )
        parser.add_argument(
            "-o",
            "--output",
            type=str,
            default=None,
            help="The output filename to use for the predicted data.",
        )

        # Class attributes to exclude from cli
        exclude_args = dict(region=("merge_overlapping",),)

        for name, attrs_class in POLICY_CLASSES.items():
            add_class_args(parser, attrs_class, name, exclude_args)

        tracking.Tracker.add_cli_parser_args(parser, arg_scope="tracking")

        return parser

    @classmethod
    def cli_args_to_policies(cls, args):
        policy_args = util.make_scoped_dictionary(vars(args), exclude_nones=True)
        return cls.from_policy_args(policy_args)

    @classmethod
    def from_policy_args(cls, policy_args):
        policy_args["region"]["merge_overlapping"] = True

        inferred_box_length = 160  # default if not set by user or inferrable

        policies = dict()

        model_type_policy_key_map = {
            model.ModelOutputType.CONFIDENCE_MAP: "confmap",
            model.ModelOutputType.PART_AFFINITY_FIELD: "paf",
            model.ModelOutputType.CENTROIDS: "centroid",
            model.ModelOutputType.TOPDOWN_CONFIDENCE_MAP: "topdown",
        }

        # Add policy classes which depend on models
        for model_path in args.models:
            training_job = job.TrainingJob.load_json(model_path)
            inference_model = model.InferenceModel.from_training_job(training_job)

            policy_key = model_type_policy_key_map[training_job.model.output_type]

            policy_object = POLICY_CLASSES[policy_key](
                inference_model=inference_model, **policy_args[policy_key]
            )

            policies[policy_key] = policy_object

            if training_job.trainer.bounding_box_size is not None:
                if training_job.trainer.bounding_box_size > 0:
                    inferred_box_length = training_job.trainer.bounding_box_size

        if not policy_args["region"].get("merged_box_length", 0):
            policy_args["region"]["merged_box_length"] = (
                policy_args["region"]["instance_box_length"] * 2
            )

        if "topdown" in policies:
            policy_args["region"]["merge_overlapping"] = False

        if "instance_box_length" not in policy_args["region"]:
            policy_args["region"]["instance_box_length"] = inferred_box_length

        # Add non-model policy classes
        non_model_policy_keys = [
            key
            for key in POLICY_CLASSES.keys()
            if key not in model_type_policy_key_map.values()
        ]
        for key in non_model_policy_keys:
            policies[key] = POLICY_CLASSES[key](**policy_args[key])

        policies["tracking"] = tracking.Tracker.make_tracker_by_name(
            **policy_args["tracking"]
        )

        return policies

    @classmethod
    def check_valid_policies(cls, policies: dict) -> bool:

        has_topdown = "topdown" in policies

        non_topdowns = [key for key in policies.keys() if key in ("confmap", "paf")]

        if has_topdown and non_topdowns:
            raise ValueError(
                f"Cannot combine topdown model with non-topdown model"
                f" {non_topdowns}."
            )

        if len(non_topdowns) == 1:
            raise ValueError(
                "Must have both CONFIDENCE_MAP and PART_AFFINITY_FIELD models."
            )

        if not has_topdown and not non_topdowns:
            raise ValueError(
                f"Must have either TOPDOWN or CONFIDENCE_MAP and PART_AFFINITY_FIELD models."
            )

        return True


if __name__ == "__main__":
    import attr

    predictor, args = Predictor.from_cli_args()

    lfs = predictor.predict(video_filename=args.data_path, frames=args.frames)

    if args.output:
        output_path = args.output
    else:
        out_dir = os.path.dirname(args.data_path)
        out_name = os.path.basename(args.data_path) + ".predictions.h5"
        output_path = os.path.join(out_dir, out_name)

    labels = Labels(labeled_frames=lfs)
    print(f"Saving: {output_path}")
    Labels.save_file(labels, output_path)
