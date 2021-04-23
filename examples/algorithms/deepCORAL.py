import torch
from models.initializer import initialize_model
from algorithms.single_model_algorithm import SingleModelAlgorithm
from wilds.common.utils import split_into_groups

class DeepCORAL(SingleModelAlgorithm):
    """
    Deep CORAL.
    This algorithm was originally proposed as an unsupervised domain adaptation algorithm.

    Original paper:
        @inproceedings{sun2016deep,
          title={Deep CORAL: Correlation alignment for deep domain adaptation},
          author={Sun, Baochen and Saenko, Kate},
          booktitle={European Conference on Computer Vision},
          pages={443--450},
          year={2016},
          organization={Springer}
        }

    The original CORAL loss is the distance between second-order statistics (covariances)
    of the source and target feature (course-grained).

    The CORAL penalty function below is adapted from DomainBed's implementation (fine-grained):
    https://github.com/facebookresearch/DomainBed/blob/1a61f7ff44b02776619803a1dd12f952528ca531/domainbed/algorithms.py#L539
    """
    def __init__(self, config, d_out, grouper, loss, metric, n_train_steps):
        # check config
        assert config.train_loader == 'group'
        assert config.uniform_over_groups
        assert config.distinct_groups
        # initialize models
        featurizer, classifier = initialize_model(config, d_out=d_out, is_featurizer=True)
        featurizer = featurizer.to(config.device)
        classifier = classifier.to(config.device)
        model = torch.nn.Sequential(featurizer, classifier).to(config.device)
        # initialize module
        super().__init__(
            config=config,
            model=model,
            grouper=grouper,
            loss=loss,
            metric=metric,
            n_train_steps=n_train_steps,
        )
        # algorithm hyperparameters
        self.penalty_weight = config.coral_penalty_weight
        # additional logging
        self.logged_fields.append('penalty')
        # set model components
        self.featurizer = featurizer
        self.classifier = classifier

    def coral_penalty(self, x, y):
        if x.dim() > 2:
            # featurizers output Tensors of size (batch_size, ..., feature dimensionality).
            # we flatten to Tensors of size (*, feature dimensionality)
            x = x.view(-1, x.size(-1))
            y = y.view(-1, y.size(-1))

        mean_x = x.mean(0, keepdim=True)
        mean_y = y.mean(0, keepdim=True)
        cent_x = x - mean_x
        cent_y = y - mean_y
        cova_x = (cent_x.t() @ cent_x) / (len(x) - 1)
        cova_y = (cent_y.t() @ cent_y) / (len(y) - 1)

        mean_diff = (mean_x - mean_y).pow(2).mean()
        cova_diff = (cova_x - cova_y).pow(2).mean()

        return mean_diff+cova_diff

    def process_batch(self, batch, unlabeled_batch=None):
        """
        Override
        """
        # forward pass
        x, y_true, metadata = batch
        x = x.to(self.device)
        y_true = y_true.to(self.device)
        g = self.grouper.metadata_to_group(metadata).to(self.device)
        features = self.featurizer(x)
        outputs = self.classifier(features)

        # package the results
        results = {
            'g': g,
            'y_true': y_true,
            'y_pred': outputs,
            'metadata': metadata,
            'features': features,
        }
        if unlabeled_batch is not None:
            x, metadata = unlabeled_batch
            x = x.to(self.device)
            results['unlabeled_features'] = self.featurizer(x)
            results['unlabeled_g'] = self.grouper.metadata_to_group(metadata).to(self.device)
        return results

    def objective(self, results):
        # extract features
        labeled_features = results.pop('features')

        if self.is_training:
            # split into groups
            unique_groups, group_indices, _ = split_into_groups(results['g'])
            n_groups_per_batch = unique_groups.numel()

            if 'unlabeled_features' in results:
                unlabeled_features = results.pop('unlabeled_features')
                unlabeled_unique_groups, unlabeled_group_indices, _ = split_into_groups(results['unlabeled_g'])
                unlabeled_groups_per_batch = unlabeled_unique_groups.numel()
                group_indices += unlabeled_group_indices
            else:
                unlabeled_groups_per_batch = 0

            # compute penalty - perform pairwise comparisons between features of all the groups
            penalty = torch.zeros(1, device=self.device)
            total_groups_per_batch = n_groups_per_batch + unlabeled_groups_per_batch
            for i_group in range(total_groups_per_batch):
                for j_group in range(i_group+1, total_groups_per_batch):
                    i_features = labeled_features if i_group < n_groups_per_batch else unlabeled_features
                    j_features = labeled_features if j_group < n_groups_per_batch else unlabeled_features
                    penalty += self.coral_penalty(i_features[group_indices[i_group]], j_features[group_indices[j_group]])
            if total_groups_per_batch > 1:
                penalty /= (total_groups_per_batch * (total_groups_per_batch-1) / 2) # get the mean penalty
        else:
            penalty = 0.

        # save penalty
        if isinstance(penalty, torch.Tensor):
            results['penalty'] = penalty.item()
        else:
            results['penalty'] = penalty

        avg_loss = self.loss.compute(results['y_pred'], results['y_true'], return_dict=False)
        return avg_loss + penalty * self.penalty_weight
